
require 'date' if RUBY_VERSION < '1.9.0'
require 'thread'

require 'fugit'


module Rufus; end

class Rufus::Scheduler

  VERSION = '3.9.2'

  EoTime = ::EtOrbi::EoTime

  require 'rufus/scheduler/util'
  require 'rufus/scheduler/jobs_core'
  require 'rufus/scheduler/jobs_one_time'
  require 'rufus/scheduler/jobs_repeat'
  require 'rufus/scheduler/job_array'
  require 'rufus/scheduler/locks'
  require 'rufus/scheduler/handoff'

  #
  # A common error class for rufus-scheduler
  #
  class Error < StandardError; end

  #
  # This error is thrown when the :timeout attribute triggers
  #
  class TimeoutError < Error; end

  #
  # For when the scheduler is not running
  # (it got shut down or didn't start because of a lock)
  #
  class NotRunningError < Error; end

  #MIN_WORK_THREADS = 3
  MAX_WORK_THREADS = 28

  attr_accessor :frequency
  attr_accessor :discard_past

  attr_reader :started_at
  attr_reader :paused_at
  attr_reader :thread
  attr_reader :thread_key
  attr_reader :mutexes
  attr_reader :last_handoff_receipt

  #attr_accessor :min_work_threads
  attr_accessor :max_work_threads

  attr_accessor :stderr

  attr_reader :work_queue

  def initialize(opts={})

    @opts = opts

    @started_at = nil
    @paused_at = nil

    @jobs = JobArray.new

    @frequency = Rufus::Scheduler.parse(opts[:frequency] || 0.300)
    @discard_past = opts.has_key?(:discard_past) ? opts[:discard_past] : true

    @mutexes = {}

    @work_queue = Queue.new
    @join_queue = Queue.new

    @handoff_mutex = Mutex.new
    @trigger_mutex = Mutex.new
    @handoff_windows = []
    @handoff_records = {}
    @handoff_sequence = 0
    @handoff_shutting_down = false
    @handoff_started_at = nil
    @handoff_cutoff_at = nil
    @handoff_deadline_at = nil
    @handoff_late_confirmations = []
    @last_handoff_receipt = nil

    #@min_work_threads =
    #  opts[:min_work_threads] || opts[:min_worker_threads] ||
    #  MIN_WORK_THREADS
    @max_work_threads =
      opts[:max_work_threads] || opts[:max_worker_threads] ||
      MAX_WORK_THREADS

    @stderr = $stderr

    @thread_key = "rufus_scheduler_#{self.object_id}"

    @scheduler_lock =
      if lockfile = opts[:lockfile]
        Rufus::Scheduler::FileLock.new(lockfile)
      else
        opts[:scheduler_lock] || Rufus::Scheduler::NullLock.new
      end

    @trigger_lock = opts[:trigger_lock] || Rufus::Scheduler::NullLock.new

    # If we can't grab the @scheduler_lock, don't run.
    lock || return

    start
  end

  # Returns a singleton Rufus::Scheduler instance
  #
  def self.singleton(opts={})

    @singleton ||= Rufus::Scheduler.new(opts)
  end

  # Alias for Rufus::Scheduler.singleton
  #
  def self.s(opts={}); singleton(opts); end

  # Releasing the gem would probably require redirecting .start_new to
  # .new and emit a simple deprecation message.
  #
  # For now, let's assume the people pointing at rufus-scheduler/master
  # on GitHub know what they do...
  #
  def self.start_new

    fail 'this is rufus-scheduler 3.x, use .new instead of .start_new'
  end

  def uptime

    @started_at ? EoTime.now - @started_at : nil
  end

  def around_trigger(job)

    yield
  end

  def uptime_s

    uptime ? self.class.to_duration(uptime) : ''
  end

  def join(time_limit=nil)

    fail NotRunningError.new('cannot join scheduler that is not running') \
      unless @thread
    fail ThreadError.new('scheduler thread cannot join itself') \
      if @thread == Thread.current

    if time_limit
      time_limit_join(time_limit)
    else
      no_time_limit_join
    end
  end

  def down?

    ! @started_at
  end

  def up?

    !! @started_at
  end

  def paused?

    !! @paused_at
  end

  def pause

    @paused_at = EoTime.now
  end

  def resume(opts={})

    dp = opts[:discard_past]
    jobs.each { |job| job.resume_discard_past = dp }

    @paused_at = nil
  end

  #--
  # scheduling methods
  #++

  def at(time, callable=nil, opts={}, &block)

    do_schedule(:once, time, callable, opts, opts[:job], block)
  end

  def schedule_at(time, callable=nil, opts={}, &block)

    do_schedule(:once, time, callable, opts, true, block)
  end

  def in(duration, callable=nil, opts={}, &block)

    do_schedule(:once, duration, callable, opts, opts[:job], block)
  end

  def schedule_in(duration, callable=nil, opts={}, &block)

    do_schedule(:once, duration, callable, opts, true, block)
  end

  def every(duration, callable=nil, opts={}, &block)

    do_schedule(:every, duration, callable, opts, opts[:job], block)
  end

  def schedule_every(duration, callable=nil, opts={}, &block)

    do_schedule(:every, duration, callable, opts, true, block)
  end

  def interval(duration, callable=nil, opts={}, &block)

    do_schedule(:interval, duration, callable, opts, opts[:job], block)
  end

  def schedule_interval(duration, callable=nil, opts={}, &block)

    do_schedule(:interval, duration, callable, opts, true, block)
  end

  def cron(cronline, callable=nil, opts={}, &block)

    do_schedule(:cron, cronline, callable, opts, opts[:job], block)
  end

  def schedule_cron(cronline, callable=nil, opts={}, &block)

    do_schedule(:cron, cronline, callable, opts, true, block)
  end

  def schedule(arg, callable=nil, opts={}, &block)

    callable, opts = nil, callable if callable.is_a?(Hash)
    opts = opts.dup

    opts[:_t] = Rufus::Scheduler.parse(arg, opts)

    case opts[:_t]
    when ::Fugit::Cron then schedule_cron(arg, callable, opts, &block)
    when ::EtOrbi::EoTime, Time then schedule_at(arg, callable, opts, &block)
    else schedule_in(arg, callable, opts, &block)
    end
  end

  def repeat(arg, callable=nil, opts={}, &block)

    callable, opts = nil, callable if callable.is_a?(Hash)
    opts = opts.dup

    opts[:_t] = Rufus::Scheduler.parse(arg, opts)

    case opts[:_t]
    when ::Fugit::Cron then schedule_cron(arg, callable, opts, &block)
    else schedule_every(arg, callable, opts, &block)
    end
  end

  def unschedule(job_or_job_id)

    job, job_id = fetch(job_or_job_id)

    fail ArgumentError.new("no job found with id '#{job_id}'") unless job

    job.unschedule if job
  end

  #--
  # jobs methods
  #++

  # Returns all the scheduled jobs
  # (even those right before re-schedule).
  #
  def jobs(opts={})

    opts = { opts => true } if opts.is_a?(Symbol)

    jobs = @jobs.to_a

    if opts[:running]
      jobs = jobs.select { |j| j.running? }
    elsif ! opts[:all]
      jobs = jobs.reject { |j| j.next_time.nil? || j.unscheduled_at }
    end

    tags = Array(opts[:tag] || opts[:tags]).collect(&:to_s)
    jobs = jobs.reject { |j| tags.find { |t| ! j.tags.include?(t) } }

    jobs
  end

  def at_jobs(opts={})

    jobs(opts).select { |j| j.is_a?(Rufus::Scheduler::AtJob) }
  end

  def in_jobs(opts={})

    jobs(opts).select { |j| j.is_a?(Rufus::Scheduler::InJob) }
  end

  def every_jobs(opts={})

    jobs(opts).select { |j| j.is_a?(Rufus::Scheduler::EveryJob) }
  end

  def interval_jobs(opts={})

    jobs(opts).select { |j| j.is_a?(Rufus::Scheduler::IntervalJob) }
  end

  def cron_jobs(opts={})

    jobs(opts).select { |j| j.is_a?(Rufus::Scheduler::CronJob) }
  end

  def job(job_id)

    @jobs[job_id]
  end

  # Returns true if the scheduler has acquired the [exclusive] lock and
  # thus may run.
  #
  # Most of the time, a scheduler is run alone and this method should
  # return true. It is useful in cases where among a group of applications
  # only one of them should run the scheduler. For schedulers that should
  # not run, the method should return false.
  #
  # Out of the box, rufus-scheduler proposes the
  # :lockfile => 'path/to/lock/file' scheduler start option. It makes
  # it easy for schedulers on the same machine to determine which should
  # run (the first to write the lockfile and lock it). It uses "man 2 flock"
  # so it probably won't work reliably on distributed file systems.
  #
  # If one needs to use a special/different locking mechanism, the scheduler
  # accepts :scheduler_lock => lock_object. lock_object only needs to respond
  # to #lock
  # and #unlock, and both of these methods should be idempotent.
  #
  # Look at rufus/scheduler/locks.rb for an example.
  #
  def lock

    @scheduler_lock.lock
  end

  # Sister method to #lock, is called when the scheduler shuts down.
  #
  def unlock

    @trigger_lock.unlock
    @scheduler_lock.unlock
  end

  # Callback called when a job is triggered. If the lock cannot be acquired,
  # the job won't run (though it'll still be scheduled to run again if
  # necessary).
  #
  def confirm_lock

    @trigger_lock.lock
  end

  # Returns true if this job is currently scheduled.
  #
  # Takes extra care to answer true if the job is a repeat job
  # currently firing.
  #
  def scheduled?(job_or_job_id)

    job, _ = fetch(job_or_job_id)

    !! (job && job.unscheduled_at.nil? && job.next_time != nil)
  end

  # Lists all the threads associated with this scheduler.
  #
  def threads

    Thread.list.select { |t| t[thread_key] }
  end

  # Lists all the work threads (the ones actually running the scheduled
  # block code)
  #
  # Accepts a query option, which can be set to:
  # * :all (default), returns all the threads that are work threads
  #   or are currently running a job
  # * :active, returns all threads that are currently running a job
  # * :vacant, returns the threads that are not running a job
  #
  # If, thanks to :blocking => true, a job is scheduled to monopolize the
  # main scheduler thread, that thread will get returned when :active or
  # :all.
  #
  def work_threads(query=:all)

    ts = threads.select { |t| t[:rufus_scheduler_work_thread] }

    case query
    when :active then ts.select { |t| t[:rufus_scheduler_job] }
    when :vacant then ts.reject { |t| t[:rufus_scheduler_job] }
    else ts
    end
  end

  def running_jobs(opts={})

    jobs(opts.merge(:running => true))
  end

  def occurrences(time0, time1, format=:per_job)

    h = {}

    jobs.each do |j|
      os = j.occurrences(time0, time1)
      h[j] = os if os.any?
    end

    if format == :timeline
      a = []
      h.each { |j, ts| ts.each { |t| a << [ t, j ] } }
      a.sort_by { |(t, _)| t }
    else
      h
    end
  end

  def timeline(time0, time1)

    occurrences(time0, time1, :timeline)
  end

  def on_error(job, err)

    pre = err.object_id.to_s

    ms = {}; mutexes.each { |k, v| ms[k] = v.locked? }

    stderr.puts("{ #{pre} rufus-scheduler intercepted an error:")
    if job
      stderr.puts("  #{pre}   job:")
      stderr.puts("  #{pre}     #{job.class} #{job.original.inspect} #{job.opts.inspect}")
      stderr.puts("  #{pre}     #{job.source_location.inspect}")
      # TODO: eventually use a Job#detail or something like that
    else
      stderr.puts("  #{pre}   job: (error did not occur in a job)")
    end
    stderr.puts("  #{pre}   error:")
    stderr.puts("  #{pre}     #{err.object_id}")
    stderr.puts("  #{pre}     #{err.class}")
    stderr.puts("  #{pre}     #{err}")
    err.backtrace.each do |l|
      stderr.puts("  #{pre}       #{l}")
    end
    stderr.puts("  #{pre}   tz:")
    stderr.puts("  #{pre}     ENV['TZ']: #{ENV['TZ']}")
    stderr.puts("  #{pre}     Time.now: #{Time.now}")
    stderr.puts("  #{pre}     local_tzone: #{EoTime.local_tzone.inspect}")
    stderr.puts("  #{pre}   et-orbi:")
    stderr.puts("  #{pre}     #{EoTime.platform_info}")
    stderr.puts("  #{pre}   scheduler:")
    stderr.puts("  #{pre}     object_id: #{object_id}")
    stderr.puts("  #{pre}     opts:")
    stderr.puts("  #{pre}       #{@opts.inspect}")
    stderr.puts("  #{pre}       frequency: #{self.frequency}")
    stderr.puts("  #{pre}       scheduler_lock: #{@scheduler_lock.inspect}")
    stderr.puts("  #{pre}       trigger_lock: #{@trigger_lock.inspect}")
    stderr.puts("  #{pre}     uptime: #{uptime} (#{uptime_s})")
    stderr.puts("  #{pre}     down?: #{down?}")
    stderr.puts("  #{pre}     frequency: #{frequency.inspect}")
    stderr.puts("  #{pre}     discard_past: #{discard_past.inspect}")
    stderr.puts("  #{pre}     started_at: #{started_at.inspect}")
    stderr.puts("  #{pre}     paused_at: #{paused_at.inspect}")
    stderr.puts("  #{pre}     threads: #{self.threads.size}")
    stderr.puts("  #{pre}       thread: #{self.thread}")
    stderr.puts("  #{pre}       thread_key: #{self.thread_key}")
    stderr.puts("  #{pre}       work_threads: #{work_threads.size}")
    stderr.puts("  #{pre}         active: #{work_threads(:active).size}")
    stderr.puts("  #{pre}         vacant: #{work_threads(:vacant).size}")
    stderr.puts("  #{pre}         max_work_threads: #{max_work_threads}")
    stderr.puts("  #{pre}       mutexes: #{ms.inspect}")
    stderr.puts("  #{pre}     jobs: #{jobs.size}")
    stderr.puts("  #{pre}       at_jobs: #{at_jobs.size}")
    stderr.puts("  #{pre}       in_jobs: #{in_jobs.size}")
    stderr.puts("  #{pre}       every_jobs: #{every_jobs.size}")
    stderr.puts("  #{pre}       interval_jobs: #{interval_jobs.size}")
    stderr.puts("  #{pre}       cron_jobs: #{cron_jobs.size}")
    stderr.puts("  #{pre}     running_jobs: #{running_jobs.size}")
    stderr.puts("  #{pre}     work_queue:")
    stderr.puts("  #{pre}       size: #{@work_queue.size}")
    stderr.puts("  #{pre}       num_waiting: #{@work_queue.num_waiting}")
    stderr.puts("  #{pre}     join_queue:")
    stderr.puts("  #{pre}       size: #{@join_queue.size}")
    stderr.puts("  #{pre}       num_waiting: #{@join_queue.num_waiting}")
    stderr.puts("} #{pre} .")

  rescue => e

    stderr.puts("failure in #on_error itself:")
    stderr.puts(e.inspect)
    stderr.puts(e.backtrace)

  ensure

    stderr.flush
  end

  def shutdown(opt=nil, *args)

    opts =
      case opt
      when Symbol then { opt => true }
      when Hash then opt.dup
      when nil then {}
      else opt.respond_to?(:to_hash) ? opt.to_hash.dup : { opt => true }
      end

    args.each { |a| opts.merge!(a) if a.is_a?(Hash) }

    handoff_receipt =
      if opts[:handoff] || opts[:receipt] || opts[:drain]
        opts[:handoff] ||= true if opts[:drain]
        handoff_shutdown(opts)
      end

    unless handoff_receipt

      @jobs.unschedule_all

      if opts[:wait] || opts[:join]
        join_shutdown(opts)
      elsif opts[:kill]
        kill_shutdown(opts)
      else
        regular_shutdown(opts)
      end

      @work_queue.clear

      unlock

      @thread.join unless @thread == Thread.current
    end

    handoff_receipt
  end
  alias stop shutdown

  #
  # Shuts the scheduler down and returns the handoff receipt.
  #
  def handoff(opts=nil)

    opts = {} if opts.nil?
    opts = { :wait => opts } if opts.is_a?(Numeric) || opts.is_a?(String)

    shutdown(opts.merge(:handoff => true))
  end
  alias drain handoff
  alias handoff_shutdown! handoff

  #
  # Returns the receipt of the last handoff shutdown, or nil.
  #
  # The receipt is a frozen snapshot, reading it twice yields the same
  # classification even if a job confirms its delivery late.
  #
  def handoff_receipt

    @last_handoff_receipt
  end
  alias last_handoff_receipt handoff_receipt
  alias handoff_manifest handoff_receipt

  #
  # Returns the latest internal view of the windows known to this scheduler.
  #
  # Unlike #handoff_receipt, this view is live: a late delivery confirmation
  # is reflected here. Receipts already returned are not modified.
  #
  def handoff_windows

    @handoff_mutex.synchronize {
      @handoff_windows.map { |run| run.snapshot(@handoff_deadline_at) }
    }
  end

  #
  # Returns the windows of the last receipt that got confirmed after that
  # receipt was returned.
  #
  def late_handoff_confirmations

    @handoff_mutex.synchronize { @handoff_late_confirmations.dup }
  end

  #
  # True while a handoff shutdown is in progress.
  #
  def handoff_draining?

    @handoff_shutting_down
  end

  #
  # Confirms that the data for a window was delivered.
  #
  # job_or_job_id:: the job (or its id)
  # window_at::     the planned time of the window (optional)
  # detail::        an optional payload describing the delivery
  #
  # Returns the confirmed HandoffWindow snapshot, or nil if no open window
  # matched.
  #
  def confirm_delivery(job_or_job_id, window_at=nil, detail=nil)

    if detail.nil? && window_at.is_a?(Hash)
      detail = window_at
      window_at = nil
    elsif detail.nil? && window_at.is_a?(String)
      parsed = Handoff.parse_time(window_at)
      if parsed
        window_at = parsed
      else
        detail = window_at
        window_at = nil
      end
    end

    job, job_id = fetch(job_or_job_id)
    job_id = job_id.to_s

    run = nil

    @handoff_mutex.synchronize do

      candidates = @handoff_windows.select { |w|
        w.job_id == job_id && w.open?
      }

      if window_at
        wanted = Handoff.eo_time(window_at)
        candidates = candidates.select { |w|
          w.planned_at && wanted &&
          (w.planned_at.to_f - wanted.to_f).abs < 0.000001
        }
      end

      run = candidates.last

      run.confirm!(detail) if run
    end

    return nil unless run

    snapshot = run.snapshot

    if @last_handoff_receipt
      reported = @last_handoff_receipt.windows.find { |w| w.id == snapshot.id }
      if reported && !reported.completed?
        @handoff_mutex.synchronize {
          @handoff_late_confirmations << snapshot
        }
      end
    end

    snapshot
  end
  alias confirm_handoff confirm_delivery

  #
  # Registers a window about to be handed to a worker. Called by Job.
  #
  def handoff_register_window(job, planned_at)

    planned_at = Handoff.eo_time(planned_at)
    return nil unless planned_at

    @handoff_mutex.synchronize do

      key = handoff_window_key(job.job_id, planned_at)

      run = @handoff_records[key]
      return run if run

      run = Handoff::WindowRun.new(
        self, job, planned_at, @handoff_sequence = (@handoff_sequence || 0) + 1,
        :queued)

      @handoff_records[key] = run
      @handoff_windows << run

      handoff_prune_windows

      run
    end
  end

  def handoff_mark_started(run)

    return nil unless run

    @handoff_mutex.synchronize { run.start! }

    run
  end

  def handoff_mark_finished(run, error=nil)

    return nil unless run

    @handoff_mutex.synchronize { run.finish!(error) }

    run
  end

  def handoff_mark_not_started(run, reason=:not_started)

    return nil unless run

    @handoff_mutex.synchronize { run.not_started!(reason) }

    run
  end

  def handoff_window_for(job, planned_at)

    planned_at = Handoff.eo_time(planned_at)
    return nil unless planned_at

    @handoff_mutex.synchronize {
      @handoff_records[handoff_window_key(job.job_id, planned_at)]
    }
  end

  protected

  def handoff_shutdown(opts)

    wait = opts[:wait] || opts[:join] || opts[:deadline]
    wait = true if wait.nil? && opts.key?(:wait)
    wait = opts.has_key?(:wait) ? opts[:wait] : wait
    wait = Handoff.parse_wait(wait)

    tags = Array(opts[:tag] || opts[:tags]).map(&:to_s)

    started_at = EoTime.now
    cutoff_at = started_at
    indefinite = (wait == :indefinite)
    wait_s = indefinite ? nil : wait
    deadline_at = wait_s.nil? ? nil : started_at + wait_s

    @handoff_mutex.synchronize do
      @handoff_shutting_down = true
      @handoff_started_at = started_at
      @handoff_cutoff_at = cutoff_at
      @handoff_deadline_at = deadline_at
      @handoff_late_confirmations = []
    end

    # Let the scheduler thread finish the round it may be in, then rely on
    # @handoff_shutting_down to keep it from triggering further windows.
    @trigger_mutex.synchronize { }

    unless opts[:keep_running]
      @paused_at = started_at
    end

    handoff_register_due_windows(cutoff_at, tags)

    unless opts[:keep_running]
      handoff_drain_work_queue
    end

    deadline = indefinite ? nil : monow + wait_s

    handoff_wait_for_confirmations(deadline, cutoff_at, tags, indefinite)

    handoff_mark_queued_not_started(cutoff_at, tags)

    returned_at = EoTime.now

    receipt = handoff_build_receipt(
      started_at, cutoff_at, returned_at, deadline_at, wait_s, tags)

    @handoff_mutex.synchronize do
      @last_handoff_receipt = receipt
      @handoff_shutting_down = false
    end

    receipt.write(opts[:handoff_path] || opts[:receipt_path]) \
      if opts[:handoff_path] || opts[:receipt_path]

    if opts[:keep_running]
      @paused_at = nil
      return receipt
    end

    @started_at = nil
    (work_threads.size * 2 + 1).times { @work_queue << :shutdown }

    work_threads.each(&:kill) if opts[:kill]

    @thread.join unless @thread == Thread.current

    unlock

    receipt
  end

  def handoff_register_due_windows(cutoff_at, tags)

    @jobs.to_a.each do |job|

      next if job.unscheduled_at || ! handoff_tag_match?(job, tags)

      planned_at = Handoff.eo_time(job.next_time)

      next unless planned_at
      next if planned_at > cutoff_at

      run = handoff_register_window(job, planned_at)
      handoff_mark_not_started(run, :due_but_not_started) if run && run.queued?
    end
  end

  def handoff_drain_work_queue

    loop do

      item =
        begin
          @work_queue.pop(true)
        rescue ThreadError
          nil
        end

      break unless item

      next if item == :shutdown

      job, time, run = item
      run ||= handoff_window_for(job, time)

      handoff_mark_not_started(run, :queued_but_not_started)
    end
  end

  def handoff_mark_queued_not_started(cutoff_at, tags)

    @handoff_mutex.synchronize do
      @handoff_windows.each do |run|
        next unless run.queued?
        next unless handoff_tag_match?(run.job, tags)
        next if run.planned_at && run.planned_at > cutoff_at

        run.not_started!(run.reason || :queued_but_not_started)
      end
    end
  end

  def handoff_wait_for_confirmations(deadline, cutoff_at, tags, indefinite=false)

    loop do

      pending = handoff_confirmation_pending?(cutoff_at, tags)

      break unless pending
      break if deadline && monow >= deadline

      sleep([ 0.01, deadline ? [ deadline - monow, 0 ].max : 0.01 ].min)
    end

    @handoff_mutex.synchronize { handoff_quiesce_windows(cutoff_at, tags) }
  end

  #
  # Freezes the classification of the windows in the cutoff. Runs inside
  # @handoff_mutex, so a confirmation either lands before or after the
  # freeze, never silently between "pending" and "reported".
  #
  def handoff_quiesce_windows(cutoff_at, tags)

    @handoff_windows.each do |run|
      next if run.completed?
      next unless handoff_tag_match?(run.job, tags)
      next if run.planned_at && run.planned_at > cutoff_at

      if run.started_at
        run.quiesce!
      else
        run.not_started!(run.reason || :queued_but_not_started)
      end
    end
  end

  def handoff_confirmation_pending?(cutoff_at, tags)

    @handoff_mutex.synchronize do

      @handoff_windows.any? do |run|
        next false if run.completed?
        next false unless handoff_tag_match?(run.job, tags)
        next false if run.planned_at && run.planned_at > cutoff_at

        run.running?
      end
    end
  end

  def handoff_build_receipt(started_at, cutoff_at, returned_at, deadline_at, wait, tags)

    windows = []

    @handoff_mutex.synchronize do

      @handoff_windows.each do |run|
        next unless handoff_tag_match?(run.job, tags)

        snapshot = run.snapshot(deadline_at)
        next if snapshot.planned_at && snapshot.planned_at > cutoff_at

        windows << snapshot
      end
    end

    windows.sort_by! { |w| [ w.planned_at ? w.planned_at.to_f : 0, w.job_id, w.round ] }

    Handoff::Receipt.new(
      {
        :version => 1,
        :scheduler_object_id => object_id,
        :shutdown_started_at => started_at,
        :cutoff_at => cutoff_at,
        :returned_at => returned_at,
        :deadline_at => deadline_at,
        :shutdown_started_epoch => started_at.to_f,
        :cutoff_epoch => cutoff_at.to_f,
        :returned_epoch => returned_at.to_f,
        :deadline_epoch => deadline_at && deadline_at.to_f,
        :wait => wait,
        :tags => tags
      },
      windows)
  end

  def handoff_tag_match?(job, tags)

    return true if tags.empty?

    tags.all? { |tag| job.tags.include?(tag) }
  end

  def handoff_window_key(job_id, planned_at)

    "#{job_id}@#{planned_at.to_f}"
  end

  def handoff_prune_windows

    return if @handoff_windows.size <= 10_000

    excess = @handoff_windows.size - 10_000
    removed = @handoff_windows.shift(excess)

    removed.each { |run| @handoff_records.delete(handoff_window_key(run.job_id, run.planned_at)) }
  end

  def join_shutdown(opts)

    limit = opts[:wait] || opts[:join]
    limit = limit.is_a?(Numeric) ? limit : nil

    #@started_at = nil
      #
      # when @started_at is nil, the scheduler thread exits, here
      # we want it to exit when all the work threads have been joined
      # hence it's set to nil later on
      #
    @paused_at = EoTime.now

    (work_threads.size * 2 + 1).times { @work_queue << :shutdown }

    work_threads
      .collect { |wt|
        wt == Thread.current ? nil : Thread.new { wt.join(limit); wt.kill } }
      .each { |st|
        st.join if st }

    @started_at = nil
  end

  def kill_shutdown(opts)

    @started_at = nil
    work_threads.each(&:kill)
  end

  def regular_shutdown(opts)

    @started_at = nil
  end

  def time_limit_join(limit)

    fail ArgumentError.new("limit #{limit.inspect} should be > 0") \
      unless limit.is_a?(Numeric) && limit > 0

    t0 = monow
    f = [ limit.to_f / 20, 0.100 ].min

    while monow - t0 < limit
      r =
        begin
          @join_queue.pop(true)
        rescue ThreadError
          # #<ThreadError: queue empty>
          false
        end
      return r if r
      sleep(f)
    end

    nil
  end

  def no_time_limit_join

    @join_queue.pop
  end

  # Returns [ job, job_id ]
  #
  def fetch(job_or_job_id)

    if job_or_job_id.respond_to?(:job_id)
      [ job_or_job_id, job_or_job_id.job_id ]
    else
      [ job(job_or_job_id), job_or_job_id ]
    end
  end

  def terminate_all_jobs

    jobs.each { |j| j.unschedule }

    sleep 0.01 while running_jobs.size > 0
  end

  #def free_all_work_threads
  #
  #  work_threads.each { |t| t.raise(KillSignal) }
  #end

  def start

    @started_at = EoTime.now

    @thread =
      Thread.new do

        while @started_at do
          begin

            unschedule_jobs
            trigger_jobs unless @paused_at
            timeout_jobs

            sleep(@frequency)

          rescue => err
            #
            # for `blocking: true` jobs mostly
            #
            on_error(nil, err)
          end
        end

        rejoin
      end

    @thread[@thread_key] = true
    @thread[:rufus_scheduler] = self
    @thread[:name] = @opts[:thread_name] || "#{@thread_key}_scheduler"
  end

  def unschedule_jobs

    @jobs.delete_unscheduled
  end

  def trigger_jobs

    now = EoTime.now

    @trigger_mutex.synchronize do
      return if @handoff_shutting_down

      @jobs.each(now) do |job|
        job.trigger(now)
      end
    end
  end

  def timeout_jobs

    work_threads(:active).each do |t|

      job = t[:rufus_scheduler_job]
      to = t[:rufus_scheduler_timeout]
      ts = t[:rufus_scheduler_time]

      next unless job && to && ts
        # thread might just have become inactive (job -> nil)

      to = ts + to unless to.is_a?(EoTime)

      next if to > EoTime.now

      t.raise(Rufus::Scheduler::TimeoutError)
    end
  end

  def rejoin

    (@join_queue.num_waiting * 2 + 1).times { @join_queue << @thread }
  end

  def do_schedule(job_type, t, callable, opts, return_job_instance, block)

    fail NotRunningError.new(
      'cannot schedule, scheduler is down or shutting down'
    ) if @started_at.nil?

    callable, opts = nil, callable if callable.is_a?(Hash)
    opts = opts.dup unless opts.has_key?(:_t)

    return_job_instance ||= opts[:job]

    job_class =
      case job_type
      when :once
        opts[:_t] ||= Rufus::Scheduler.parse(t, opts)
        opts[:_t].is_a?(Numeric) ? InJob : AtJob
      when :every
        EveryJob
      when :interval
        IntervalJob
      when :cron
        CronJob
      end

    job = job_class.new(self, t, opts, block || callable)

    if job.past? && (d = job.send(:discard_past?))
      fail ArgumentError.new(
        "scheduling in the past and discard_past set to :fail"
          ) if d == :fail
      return
    end

    job.check_frequency

    @jobs.push(job)

    return_job_instance ? job : job.job_id
  end

  def monow; self.class.monow; end
  def ltstamp; self.class.ltstamp; end
end

