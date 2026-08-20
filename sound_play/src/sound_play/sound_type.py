import os
import threading

import rospy

from sound_play.msg import SoundRequest

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst as Gst
except Exception:
    str = """
**************************************************************
Error opening pygst. Is gstreamer installed?
**************************************************************
"""
    rospy.logfatal(str)
    # print str
    exit(1)


class SoundType(object):
    STOPPED = 0
    LOOPING = 1
    COUNTING = 2

    # Set by soundplay_node when it wants a copy of what is being played.
    # Called as reference_cb(bytes) from the GStreamer thread, with 16 kHz
    # mono signed 16-bit samples.
    reference_cb = None

    def __init__(self, file, device, volume=1.0):
        self.lock = threading.RLock()
        self.state = self.STOPPED
        self.sound = Gst.ElementFactory.make("playbin", None)
        if self.sound is None:
            raise Exception("Could not create sound player")

        if device or SoundType.reference_cb is not None:
            self.sink = self._build_sink(device)
            self.sound.set_property("audio-sink", self.sink)

        if (":" in file):
            uri = file
        elif os.path.isfile(file):
            uri = "file://" + os.path.abspath(file)
        else:
            rospy.logerr('Error: URI is invalid: %s' % file)

        self.uri = uri
        self.volume = volume
        self.sound.set_property('uri', uri)
        self.sound.set_property("volume", volume)
        self.staleness = 1
        self.file = file

        self.bus = self.sound.get_bus()
        self.bus.add_signal_watch()
        self.bus_conn_id = self.bus.connect("message", self.on_stream_end)

    def _build_sink(self, device):
        """The audio sink, optionally tapped for an echo canceller.

        An acoustic echo canceller needs to know what came out of the speaker,
        and taking that from the sound card's monitor turned out to be the hard
        way: the monitor stream is woken on the sink's own schedule rather than
        the microphone's, so most of the reference never arrived and had to be
        invented, and the canceller then removed the person's voice along with
        the robot's. Here the signal is branched off inside the pipeline that
        plays it, so it is the same samples the speaker gets, and it is exactly
        silent when nothing is playing.

        Without a reference callback registered this is the plain sink
        sound_play always used.
        """
        speaker = Gst.ElementFactory.make("alsasink", "sink")
        if device:
            speaker.set_property("device", device)
        if SoundType.reference_cb is None:
            return speaker

        bin_ = Gst.Bin.new("tapped-sink")
        tee = Gst.ElementFactory.make("tee", None)
        # Two queues so neither branch can stall the other: a slow subscriber
        # must not make the speaker stutter.
        q_play = Gst.ElementFactory.make("queue", None)
        q_tap = Gst.ElementFactory.make("queue", None)
        conv = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        caps = Gst.ElementFactory.make("capsfilter", None)
        caps.set_property("caps", Gst.Caps.from_string(
            "audio/x-raw,format=S16LE,rate=16000,channels=1"))
        appsink = Gst.ElementFactory.make("appsink", None)
        appsink.set_property("emit-signals", True)
        appsink.set_property("sync", False)
        appsink.set_property("max-buffers", 8)
        appsink.set_property("drop", True)
        appsink.connect("new-sample", self._on_reference_sample)

        for e in (tee, q_play, speaker, q_tap, conv, resample, caps, appsink):
            bin_.add(e)
        tee.link(q_play)
        q_play.link(speaker)
        tee.link(q_tap)
        q_tap.link(conv)
        conv.link(resample)
        resample.link(caps)
        caps.link(appsink)

        bin_.add_pad(Gst.GhostPad.new("sink", tee.get_static_pad("sink")))
        return bin_

    def _on_reference_sample(self, appsink):
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                cb = SoundType.reference_cb
                if cb is not None:
                    cb(bytes(info.data))
            finally:
                buf.unmap(info)
        return Gst.FlowReturn.OK

    def on_stream_end(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            if (self.state == self.LOOPING):
                self.sound.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            else:
                self.stop()

    def __del__(self):
        # stop our GST object so that it gets garbage-collected
        self.dispose()

    def update(self):
        if self.bus is not None:
            self.bus.poll(Gst.MessageType.ERROR, 10)

    def loop(self):
        self.lock.acquire()
        try:
            self.staleness = 0
            if self.state == self.COUNTING:
                self.stop()

            if self.state == self.STOPPED:
                self.sound.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
                self.sound.set_state(Gst.State.PLAYING)
            self.state = self.LOOPING
        finally:
            self.lock.release()

    def dispose(self):
        self.lock.acquire()
        try:
            if self.bus is not None:
                self.sound.set_state(Gst.State.NULL)
                self.bus.disconnect(self.bus_conn_id)
                self.bus.remove_signal_watch()
                self.bus = None
                self.sound = None
                self.sink = None
                self.state = self.STOPPED
        except Exception as e:
            rospy.logerr('Exception in dispose: %s' % str(e))
        finally:
            self.lock.release()

    def stop(self):
        if self.state != self.STOPPED:
            self.lock.acquire()
            try:
                self.sound.set_state(Gst.State.NULL)
                self.state = self.STOPPED
            finally:
                self.lock.release()

    def single(self):
        self.lock.acquire()
        try:
            rospy.logdebug("Playing %s" % self.uri)
            self.staleness = 0
            if self.state == self.LOOPING:
                self.stop()

            self.sound.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            self.sound.set_state(Gst.State.PLAYING)
            self.state = self.COUNTING
        finally:
            self.lock.release()

    def command(self, cmd):
        if cmd == SoundRequest.PLAY_STOP:
            self.stop()
        elif cmd == SoundRequest.PLAY_ONCE:
            self.single()
        elif cmd == SoundRequest.PLAY_START:
            self.loop()

    def get_staleness(self):
        self.lock.acquire()
        position = 0
        duration = 0
        try:
            position = self.sound.query_position(Gst.Format.TIME)[1]
            duration = self.sound.query_duration(Gst.Format.TIME)[1]
        except Exception:
            position = 0
            duration = 0
        finally:
            self.lock.release()

        if position != duration:
            self.staleness = 0
        else:
            self.staleness = self.staleness + 1
        return self.staleness

    def get_playing(self):
        return self.state == self.COUNTING
