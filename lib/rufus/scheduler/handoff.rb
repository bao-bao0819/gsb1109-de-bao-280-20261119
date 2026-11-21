
require 'json'
require 'digest'

class Rufus::Scheduler

  #
  # The status of a window in a handoff receipt.
  #
  # :completed     the job confirmed that the data was delivered
  # :not_started   the window was due but no worker started it
  # :needs_review  a worker started it but there is no delivery confirmation
  #
  module Handoff

    COMPLETED = :completed
    NOT_STARTED = :not_started
    NEEDS_REVIEW = :needs_review

    STATUSES = [ COMPLETED, NOT_STARTED, NEEDS_REVIEW ].freeze

    class << self

      def eo_time(value)

        return nil if value.nil? || value == false
        return value if value.is_a?(EoTime)
        # EoTime.make(Numeric) means "now + n", not an epoch timestamp
        return EoTime.new(value.to_f, nil) if value.is_a?(Numeric)
        return EoTime.make(value) if value.is_a?(Time)

        EoTime.parse(value.to_s)
      end

      #
      # Builds an EoTime from an epoch timestamp.
      #
      def epoch_time(value)

        EoTime.new(value.to_f, nil)
      end

      #
      # Returns an EoTime if the value really looks like a time, nil otherwise.
      # Used to tell a window time from a delivery payload.
      #
      def parse_time(value)

        return nil unless value.is_a?(String)
        return nil unless value =~ /\d/
        return nil if value.strip.empty?

        begin
          EoTime.parse(value)
        rescue StandardError
          nil
        end
      end

      def time_to_s(value)

        t = eo_time(value)
        return nil unless t
        return t.iso8601(6) if t.respond_to?(:iso8601)

        Time.at(t.to_f).utc.iso8601(6)
      end
      alias timestamp time_to_s

      def json_safe(value)

        case value
        when nil, true, false, String, Numeric then value
        when Symbol then value.to_s
        when Array then value.map { |v| json_safe(v) }
        when Hash
          value.each_with_object({}) { |(k, v), h| h[json_safe(k)] = json_safe(v) }
        else
          value.respond_to?(:to_h) ? json_safe(value.to_h) : value.inspect
        end
      end

      #
      # Turns a wait option into a number of seconds or :indefinite.
      #
      # true  => :indefinite (wait until started windows settle)
      # false => 0.0 (do not wait)
      # nil   => 0.0
      # n     => n seconds
      #
      def parse_wait(value)

        return :indefinite if value == true
        return 0.0 if value == false || value.nil?
        return value.to_f if value.is_a?(Numeric)

        d = Rufus::Scheduler.parse_duration(value.to_s)
        d.nil? ? 0.0 : d
      rescue StandardError
        0.0
      end
    end

    #
    # A mutable record of one window (one scheduled occurrence) while the
    # scheduler is still alive. It is turned into a frozen HandoffWindow
    # when a receipt is built.
    #
    class WindowRun

      attr_reader :scheduler
      attr_reader :job
      attr_reader :job_id
      attr_reader :planned_at
      attr_reader :sequence
      attr_reader :started_at
      attr_reader :finished_at
      attr_reader :confirmed_at
      attr_reader :confirmation
      attr_reader :reason
      attr_reader :error
      attr_reader :quiesced_at

      def initialize(scheduler, job, planned_at, sequence, state, reason=nil)

        @scheduler = scheduler
        @job = job
        @job_id = job.job_id
        @planned_at = Handoff.eo_time(planned_at)
        @sequence = sequence.to_i
        @state = state
        @reason = reason
        @started_at = nil
        @finished_at = nil
        @confirmed_at = nil
        @confirmation = nil
        @error = nil
        @mutex = Mutex.new
      end

      def state

        @mutex.synchronize { @state }
      end
      alias status state

      def queued?

        state == :queued
      end

      def running?

        state == :running
      end

      def started?

        !! (@started_at || state == :running || state == :finished || state == :failed)
      end

      #
      # Pins the window as "started but not confirmed" once the handoff
      # deadline is reached. A later confirm! is still recorded (and shows
      # up in #late_handoff_confirmations), but not in the frozen receipt.
      #
      def quiesce!

        @mutex.synchronize do
          @state = :finished unless @state == :confirmed
          @quiesced_at ||= EoTime.now
        end

        self
      end

      def quiesced?

        @mutex.synchronize { !! @quiesced_at }
      end

      def quiesced_at

        @mutex.synchronize { @quiesced_at }
      end

      def completed?

        !! (@confirmed_at || state == :confirmed)
      end
      alias confirmed? completed?

      def open?

        ! completed?
      end

      def start!

        @mutex.synchronize do
          @started_at ||= EoTime.now
          @state = :running unless @state == :confirmed
        end

        self
      end
      alias running! start!

      def not_started!(reason=:not_started)

        @mutex.synchronize do
          unless @started_at || @state == :confirmed
            @state = :not_started
            @reason = reason
          end
        end

        self
      end
      alias pending! not_started!

      def confirm!(detail=nil)

        @mutex.synchronize do
          @confirmed_at ||= EoTime.now
          @confirmation = detail unless detail.nil?
          @state = :confirmed
        end

        self
      end
      alias delivered! confirm!

      def finish!(err=nil)

        @mutex.synchronize do
          @finished_at ||= EoTime.now
          @error = err if err
          if @state != :confirmed
            @state = err ? :failed : :finished
          end
        end

        self
      end
      alias finished! finish!

      def snapshot(deadline_at=nil)

        @mutex.synchronize do

          deadline_at = Handoff.eo_time(deadline_at)

          status, reason =
            if @confirmed_at &&
               (deadline_at.nil? || @confirmed_at.to_f <= deadline_at.to_f + 0.000001)
              [ COMPLETED, :delivery_confirmed ]
            elsif @confirmed_at
              [ NEEDS_REVIEW, :delivery_confirmed_after_deadline ]
            elsif @started_at
              [
                NEEDS_REVIEW,
                @state == :failed ?
                  :execution_failed_without_delivery_confirmation :
                  (@finished_at ?
                    :worker_finished_without_delivery_confirmation :
                    :started_without_delivery_confirmation)
              ]
            else
              [ NOT_STARTED, @reason || :not_started ]
            end

          HandoffWindow.new(
            :job_id => @job_id,
            :job_class => @job.class.name,
            :tags => @job.tags,
            :planned_at => @planned_at,
            :started_at => @started_at,
            :finished_at => @finished_at,
            :confirmed_at => @confirmed_at,
            :confirmation => @confirmation,
            :status => status,
            :reason => reason,
            :round => @sequence,
            :error => @error && @error.message
          )
        end
      end
    end

    #
    # An immutable description of one window in a handoff receipt.
    #
    class HandoffWindow

      attr_reader :job_id
      attr_reader :job_class
      attr_reader :tags
      attr_reader :planned_at
      attr_reader :started_at
      attr_reader :finished_at
      attr_reader :confirmed_at
      attr_reader :confirmation
      attr_reader :status
      attr_reader :reason
      attr_reader :round
      attr_reader :error

      def initialize(attrs)

        @job_id = attrs[:job_id].to_s
        @job_class = attrs[:job_class] && attrs[:job_class].to_s
        @tags = Array(attrs[:tags]).map(&:to_s).freeze
        @planned_at = canonical_time(attrs[:planned_at], attrs[:planned_epoch])
        @started_at = canonical_time(attrs[:started_at], attrs[:started_epoch])
        @finished_at = canonical_time(attrs[:finished_at], attrs[:finished_epoch])
        @confirmed_at = canonical_time(attrs[:confirmed_at], attrs[:confirmed_epoch])
        @confirmation = attrs[:confirmation]
        @status = attrs[:status].to_sym
        @reason = attrs[:reason] && attrs[:reason].to_sym
        @round = attrs[:round]
        @error = attrs[:error]

        freeze
      end

      def canonical_time(value, epoch=nil)

        t =
          if epoch
            Handoff.epoch_time(epoch)
          else
            Handoff.eo_time(value)
          end

        t && Handoff.epoch_time(t.to_f)
      end
      private :canonical_time

      def window_at

        @planned_at
      end
      alias scheduled_for window_at
      alias at window_at

      def completed?

        @status == COMPLETED
      end
      alias delivered? completed?
      alias confirmed? completed?

      def not_started?

        @status == NOT_STARTED
      end

      def needs_review?

        @status == NEEDS_REVIEW
      end
      alias unconfirmed? needs_review?
      alias requires_review? needs_review?

      def pending?

        not_started? || needs_review?
      end

      def reassignable?

        not_started?
      end
      alias reassign? reassignable?

      def retryable?

        not_started?
      end

      def id

        "#{@job_id}@#{@planned_at ? @planned_at.to_f : 'nil'}"
      end

      def to_h

        {
          :id => id,
          :job_id => @job_id,
          :job_class => @job_class,
          :tags => @tags.dup,
          :planned_at => Handoff.timestamp(@planned_at),
          :window_at => Handoff.timestamp(@planned_at),
          :started_at => Handoff.timestamp(@started_at),
          :finished_at => Handoff.timestamp(@finished_at),
          :confirmed_at => Handoff.timestamp(@confirmed_at),
          :planned_epoch => @planned_at && @planned_at.to_f,
          :started_epoch => @started_at && @started_at.to_f,
          :finished_epoch => @finished_at && @finished_at.to_f,
          :confirmed_epoch => @confirmed_at && @confirmed_at.to_f,
          :confirmation => Handoff.json_safe(@confirmation),
          :status => @status,
          :reason => @reason,
          :round => @round,
          :reassignable => reassignable?,
          :retryable => retryable?,
          :requires_review => needs_review?,
          :error => @error
        }
      end
      alias to_hash to_h

      def [](key)

        to_h[key.to_sym]
      end

      def fetch(key, *default, &block)

        to_h.fetch(key.to_sym, *default, &block)
      end

      def key?(key)

        to_h.has_key?(key.to_sym)
      end
      alias has_key? key?

      def keys

        to_h.keys
      end

      def values

        to_h.values
      end

      def each(&block)

        to_h.each(&block)
      end
      alias each_pair each

      def to_json(*args)

        ::JSON.generate(to_h, *args)
      end

      def ==(other)

        other.is_a?(HandoffWindow) && to_h == other.to_h
      end
      alias eql? ==

      def hash

        to_h.hash
      end
    end

    #
    # The handoff receipt returned by Scheduler#shutdown(:handoff).
    #
    # It is a frozen snapshot. A late confirmation may update the scheduler's
    # internal window records, but it cannot change a receipt that has already
    # been returned.
    #
    class Receipt

      attr_reader :windows
      attr_reader :completed
      attr_reader :not_started
      attr_reader :needs_review
      attr_reader :reassignable
      attr_reader :shutdown_started_at
      attr_reader :cutoff_at
      attr_reader :returned_at
      attr_reader :deadline_at
      attr_reader :wait
      attr_reader :tags
      attr_reader :scheduler_object_id
      attr_reader :version

      def initialize(attrs, windows)

        @version = attrs[:version] || 1
        @scheduler_object_id = attrs[:scheduler_object_id]
        @shutdown_started_at = canonical_time(attrs[:shutdown_started_at], attrs[:shutdown_started_epoch])
        @cutoff_at = canonical_time(attrs[:cutoff_at], attrs[:cutoff_epoch])
        @returned_at = canonical_time(attrs[:returned_at], attrs[:returned_epoch])
        @deadline_at = canonical_time(attrs[:deadline_at], attrs[:deadline_epoch])
        @wait = attrs[:wait]
        @tags = Array(attrs[:tags]).map(&:to_s).freeze

        @windows = windows.dup.freeze
        @completed = @windows.select(&:completed?).freeze
        @not_started = @windows.select(&:not_started?).freeze
        @needs_review = @windows.select(&:needs_review?).freeze
        @reassignable = @not_started.freeze

        freeze
      end

      alias manifest windows
      alias entries windows

      def canonical_time(value, epoch=nil)

        t =
          if epoch
            Handoff.epoch_time(epoch)
          else
            Handoff.eo_time(value)
          end

        t && Handoff.epoch_time(t.to_f)
      end
      private :canonical_time

      def pending

        @not_started + @needs_review
      end
      alias unresolved pending

      def unconfirmed

        @needs_review
      end

      def requires_review

        @needs_review
      end
      alias review needs_review

      def completed_windows

        @completed
      end

      def not_started_windows

        @not_started
      end

      def needs_review_windows

        @needs_review
      end

      def counts

        {
          :completed => @completed.size,
          :not_started => @not_started.size,
          :needs_review => @needs_review.size,
          :total => @windows.size
        }.freeze
      end
      alias summary counts

      def size

        @windows.size
      end
      alias length size

      def empty?

        @windows.empty?
      end

      def each(&block)

        @windows.each(&block)
      end

      def each_window(&block)

        @windows.each(&block)
      end

      def [](key)

        case key.to_sym
        when :windows, :manifest, :entries then @windows
        when :completed, :completed_windows, :done then @completed
        when :not_started, :not_started_windows, :pending, :unstarted
          key.to_sym == :pending ? pending : @not_started
        when :needs_review, :needs_review_windows, :unconfirmed,
             :requires_review, :review
          @needs_review
        when :reassignable, :reassign, :retryable then @reassignable
        when :counts, :summary then counts
        when :version then @version
        when :scheduler_object_id then @scheduler_object_id
        when :shutdown_started_at then @shutdown_started_at
        when :cutoff_at then @cutoff_at
        when :returned_at then @returned_at
        when :deadline_at then @deadline_at
        when :wait then @wait
        when :tags then @tags
        when :to_h then to_h
        else nil
        end
      end

      def dig(key, *rest)

        value = self[key]
        rest.empty? ? value : value.dig(*rest)
      end

      def to_h

        {
          :version => @version,
          :scheduler_object_id => @scheduler_object_id,
          :shutdown_started_at => Handoff.timestamp(@shutdown_started_at),
          :cutoff_at => Handoff.timestamp(@cutoff_at),
          :returned_at => Handoff.timestamp(@returned_at),
          :deadline_at => Handoff.timestamp(@deadline_at),
          :shutdown_started_epoch => @shutdown_started_at && @shutdown_started_at.to_f,
          :cutoff_epoch => @cutoff_at && @cutoff_at.to_f,
          :returned_epoch => @returned_at && @returned_at.to_f,
          :deadline_epoch => @deadline_at && @deadline_at.to_f,
          :wait => @wait,
          :tags => @tags.dup,
          :counts => counts.dup,
          :windows_count => @windows.size,
          :completed_count => @completed.size,
          :not_started_count => @not_started.size,
          :needs_review_count => @needs_review.size,
          :windows => @windows.map(&:to_h),
          :completed => @completed.map(&:to_h),
          :not_started => @not_started.map(&:to_h),
          :needs_review => @needs_review.map(&:to_h),
          :reassignable => @reassignable.map(&:to_h)
        }
      end
      alias to_hash to_h

      def to_json(*args)

        ::JSON.generate(to_h, *args)
      end

      def digest

        Digest::SHA256.hexdigest(to_json)
      end
      alias checksum digest

      def write(path)

        File.open(path, 'wb') { |f| f.write(to_json) }
        path
      end

      def ==(other)

        if other.is_a?(Receipt)
          to_h == other.to_h
        elsif other.is_a?(Hash)
          to_h == other
        else
          false
        end
      end
      alias eql? ==

      def hash

        to_h.hash
      end

      class << self

        def from_h(hash)

          hash = ::JSON.parse(hash) if hash.is_a?(String)

          hash = hash.each_with_object({}) { |(k, v), h| h[k.to_sym] = v }

          ws = hash[:windows]
          unless ws
            ws = []
            [ :completed, :not_started, :needs_review ].each do |k|
              ws.concat(Array(hash[k]))
            end
          end

          windows = Array(ws).map { |w| HandoffWindow.new(symbolize(w)) }

          new(
            {
              :version => hash[:version] || 1,
              :scheduler_object_id => hash[:scheduler_object_id],
              :shutdown_started_at => hash[:shutdown_started_at],
              :cutoff_at => hash[:cutoff_at],
              :returned_at => hash[:returned_at],
              :deadline_at => hash[:deadline_at],
              :shutdown_started_epoch => hash[:shutdown_started_epoch],
              :cutoff_epoch => hash[:cutoff_epoch],
              :returned_epoch => hash[:returned_epoch],
              :deadline_epoch => hash[:deadline_epoch],
              :wait => hash[:wait],
              :tags => hash[:tags]
            },
            windows
          )
        end
        alias parse from_h

        def read(path)

          from_h(File.read(path))
        end

        def load(source)

          if source.respond_to?(:read)
            from_h(source.read)
          elsif source.is_a?(Hash)
            from_h(source)
          elsif source.is_a?(String) && File.exist?(source)
            read(source)
          else
            from_h(source)
          end
        end

        protected

        def symbolize(hash)

          hash.each_with_object({}) { |(k, v), h| h[k.to_sym] = v }
        end
      end
    end
  end

  HandoffReceipt = Handoff::Receipt
  HandoffWindow = Handoff::HandoffWindow
end
