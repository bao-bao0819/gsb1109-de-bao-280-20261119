
require 'spec_helper'


describe 'handoff shutdown' do

  before :each do

    @scheduler =
      Rufus::Scheduler.new(frequency: 0.02, max_work_threads: 1)
  end

  after :each do

    @scheduler.shutdown if @scheduler.uptime
  end

  # A completes and confirms, B starts but cannot confirm yet, C is due and
  # queued but no worker starts it. A future window must stay out.
  #
  def schedule_a_b_c_future

    a = @scheduler.schedule_in('0.02s', tag: 'batch') do |job|
      sleep 0.02
      job.confirm_delivery!('warehouse')
    end

    b = @scheduler.schedule_in('0.04s', tag: 'batch') do |job|
      $handoff_b_job = job
      sleep 2.0
    end

    c = @scheduler.schedule_in('0.04s', tag: 'batch') do
      $handoff_c_started = true
    end

    future = @scheduler.schedule_in('60s', tag: 'batch') do
    end

    wait_until { a.count > 0 }
    sleep 0.25

    [ a, b, c, future ]
  end

  def by_id(receipt)

    receipt.windows.each_with_object({}) { |w, h| h[w.job_id] = w }
  end

  describe '#shutdown(:handoff)' do

    it 'returns the three classes of due windows' do

      a, b, c, future = schedule_a_b_c_future

      receipt = @scheduler.shutdown(:handoff, wait: 0.2)
      windows = by_id(receipt)

      expect(windows[a.job_id].status).to eq(:completed)
      expect(windows[a.job_id].completed?).to eq(true)
      expect(windows[a.job_id].confirmation).to eq('warehouse')

      expect(windows[b.job_id].status).to eq(:needs_review)
      expect(windows[b.job_id].needs_review?).to eq(true)
      expect(windows[b.job_id].started_at).not_to eq(nil)
      expect(windows[b.job_id].confirmed_at).to eq(nil)

      expect(windows[c.job_id].status).to eq(:not_started)
      expect(windows[c.job_id].not_started?).to eq(true)
      expect(windows[c.job_id].reassignable?).to eq(true)

      expect(windows.keys).not_to include(future.job_id)
    end

    it 'does not take a joined worker thread as a delivery proof' do

      job = @scheduler.schedule_in('0.02s') do
        sleep 0.1
      end

      wait_until { job.count > 0 }
      sleep 0.2

      receipt = @scheduler.shutdown(:handoff, wait: 1.0)
      window = by_id(receipt)[job.job_id]

      expect(window.status).to eq(:needs_review)
      expect(window.reason).to eq(:worker_finished_without_delivery_confirmation)
    end

    it 'lets a started window confirm before the deadline' do

      job = @scheduler.schedule_in('0.02s') do |job|
        sleep 0.1
        job.confirm_delivery!('before-deadline')
      end

      wait_until { job.count > 0 }

      receipt = @scheduler.shutdown(:handoff, wait: 2.0)
      window = by_id(receipt)[job.job_id]

      expect(window.status).to eq(:completed)
      expect(window.confirmation).to eq('before-deadline')
    end

    it 'keeps a window unresolved when its confirmation is late' do

      job = @scheduler.schedule_in('0.02s') do |job|
        sleep 0.7
        job.confirm_delivery!('after-deadline')
      end

      wait_until { job.count > 0 }

      t0 = monow
      receipt = @scheduler.shutdown(:handoff, wait: 0.2)
      elapsed = monow - t0

      expect(elapsed).to be < 0.6

      window = by_id(receipt)[job.job_id]

      expect(window.status).to eq(:needs_review)
      expect(window.needs_review?).to eq(true)

      sleep 0.8

      expect(@scheduler.late_handoff_confirmations.map(&:job_id)).to eq([ job.job_id ])

      again = by_id(receipt)[job.job_id]

      expect(again.status).to eq(:needs_review)
      expect(receipt.counts[:needs_review]).to eq(1)
    end

    it 'limits the receipt to the given tag' do

      a = @scheduler.schedule_in('0.02s', tag: 'billing') do |job|
        sleep 0.02
        job.confirm_delivery!('warehouse')
      end

      other = @scheduler.schedule_in('0.02s', tag: 'other') do |job|
        job.confirm_delivery!('other')
      end

      wait_until { a.count > 0 && other.count > 0 }

      receipt = @scheduler.shutdown(:handoff, tag: 'billing', wait: 0.2)

      expect(receipt.tags).to eq(%w[ billing ])
      expect(receipt.windows.map(&:job_id)).to eq([ a.job_id ])
    end

    it 'does not reassign a needs_review window' do

      job = @scheduler.schedule_in('0.02s') { sleep 2.0 }

      wait_until { job.count > 0 }

      receipt = @scheduler.shutdown(:handoff, wait: 0.1)
      window = by_id(receipt)[job.job_id]

      expect(window.needs_review?).to eq(true)
      expect(window.reassignable?).to eq(false)
      expect(window.retryable?).to eq(false)
      expect(window[:status]).to eq(:needs_review)
      expect(window['reassignable']).to eq(false)
    end

    it 'writes the receipt for a replacement scheduler to read' do

      path = File.join(Dir.pwd, "handoff-spec-#{Process.pid}.json")

      schedule_a_b_c_future

      receipt =
        @scheduler.shutdown(:handoff, wait: 0.2, handoff_path: path)

      begin
        copy = Rufus::Scheduler::Handoff::Receipt.load(path)

        expect(copy.counts).to eq(receipt.counts)
        expect(copy.digest).to eq(receipt.digest)
        expect(copy['counts'][:not_started]).to eq(1)
      ensure
        File.delete(path) if File.exist?(path)
      end
    end

    it 'returns the same receipt when read twice' do

      a, b, c, future = schedule_a_b_c_future

      receipt = @scheduler.shutdown(:handoff, wait: 0.2)

      expect(@scheduler.handoff_receipt).to equal(receipt)
      expect(@scheduler.handoff_receipt.digest).to eq(receipt.digest)

      sleep 0.3

      expect(@scheduler.handoff_receipt).to equal(receipt)
      expect(@scheduler.handoff_receipt.to_json).to eq(receipt.to_json)
    end

    it 'survives a JSON round trip' do

      a, b, c, future = schedule_a_b_c_future

      receipt = @scheduler.shutdown(:handoff, wait: 0.2)

      copy = Rufus::Scheduler::Handoff::Receipt.from_h(receipt.to_json)

      expect(copy).to eq(receipt)
      expect(copy.counts).to eq(receipt.counts)
    end
  end

  describe 'legacy shutdowns' do

    it 'keeps #shutdown working' do

      job = @scheduler.schedule_in('60s') { }

      expect(@scheduler.shutdown).to eq(nil)
      expect(@scheduler.uptime).to eq(nil)
    end

    it 'keeps #shutdown(:wait) working' do

      seen = false

      job =
        @scheduler.schedule_in('0.02s') do
          sleep 0.2
          seen = true
        end

      wait_until { job.count > 0 }

      expect(@scheduler.shutdown(:wait)).to eq(nil)
      expect(seen).to eq(true)
    end

    it 'keeps #shutdown(:kill) working' do

      counter = 0

      job =
        @scheduler.schedule_in('0.02s') do
          sleep 1
          counter += 1
        end

      wait_until { job.count > 0 }

      expect(@scheduler.shutdown(:kill)).to eq(nil)
      expect(counter).to eq(0)
    end
  end
end
