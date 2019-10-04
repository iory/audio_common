#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: furushchev <furushchev@jsk.imi.i.u-tokyo.ac.jp>

import actionlib
import gc
import rospy
import os
import random
import re
import string
import subprocess
import tempfile
from threading import Lock
import traceback


from sound_play.msg import SoundRequest, SoundRequestAction, SoundRequestResult, SoundRequestFeedback


import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from gi.repository import GLib
Gst.init(None)


import rospkg
PKG = rospkg.RosPack()


class Sound(object):
    count = 0

    def __init__(self, device=str()):
        super(Sound, self).__init__()
        self.loop = False
        self.ret = None

        count = Sound.count
        Sound.count += 1
        self.player = Gst.ElementFactory.make("playbin", "player%d" % count)
        self.videosink = Gst.ElementFactory.make("fakesink", "videosink%d" % count)
        if device:
            self.audiosink = Gst.ElementFactory.make("alsasink", "audiosink%d" % count)
            self.audiosink.set_property("device", device)
        else:
            self.audiosink = Gst.ElementFactory.make("autoaudiosink", "audiosink%d" % count)

        self.player.set_property(
            "audio-sink", self.audiosink)
        self.player.set_property(
            "video-sink", self.videosink)

        self.bus = self.player.get_bus()
        self.bus.add_signal_watch()
        self.bus_conn_id = self.bus.connect('message', self.message_cb)

    def __del__(self):
        try:
            self.dispose()
        except:
            pass

    def dispose(self):
        ret = self.player.set_state(Gst.State.NULL)
        self.bus.remove_signal_watch()
        self.bus.disconnect(self.bus_conn_id)
        self.bus = None
        self.player = None
        self.audiosink = None
        self.videosink = None

    def __hash__(self):
        return hash(self.uri)

    def __eq__(self, other):
        return self.uri == other.uri

    @property
    def uri(self):
        uri = self.player.get_property("uri")
        if not uri:
            return None
        if uri.startswith("file://"):
            return uri[7:]
        else:
            return uri

    @uri.setter
    def uri(self, uri):
        if os.path.isfile(uri):
            uri = "file://" + os.path.abspath(uri)
        self.player.set_property("uri", uri)

    @property
    def volume(self):
        return self.player.get_property("volume")

    @volume.setter
    def volume(self, vol):
        self.player.set_property("volume", vol)

    @property
    def pos(self):
        ok, perc = self.player.query_position(Gst.Format.PERCENT)
        return min(0.0, max(int(perc) * 0.01, 1.0))

    @pos.setter
    def pos(self, p):
        self.player.seek_simple(
            Gst.Format.PERCENT, Gst.SeekFlags.FLUSH, int(p * 100))

    @property
    def status(self):
        return self.player.current_state if self.player else None

    def message_cb(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            self.stop()
        elif msg.type == Gst.MessageType.EOS:
            loop = self.loop
            self.stop()
            if loop:
                self.loop = True
                self.pos = 0.0
                self.play()

    def play(self):
        ret = self.player.set_state(Gst.State.PLAYING)
        self.ret = ret
        if ret == Gst.StateChangeReturn.FAILURE:
            rospy.logerr("Failed to play %s" % self.uri)
            return False
        return True

    def stop(self):
        self.loop = False
        ret = self.player.set_state(Gst.State.NULL)
        if ret == Gst.StateChangeReturn.FAILURE:
            rospy.logerr("Failed to stop %s" % self.uri)
            return

    def poll(self):
        self.bus.poll(Gst.MessageType.ERROR, 10)


class SoundPlayNode(object):
    sounds_dir = os.path.join(PKG.get_path("sound_play"), "sounds")
    builtin_sounds = {
        SoundRequest.BACKINGUP              : (os.path.join(sounds_dir, 'BACKINGUP.ogg'), 0.1),
        SoundRequest.NEEDS_UNPLUGGING       : (os.path.join(sounds_dir, 'NEEDS_UNPLUGGING.ogg'), 1),
        SoundRequest.NEEDS_PLUGGING         : (os.path.join(sounds_dir, 'NEEDS_PLUGGING.ogg'), 1),
        SoundRequest.NEEDS_UNPLUGGING_BADLY : (os.path.join(sounds_dir, 'NEEDS_UNPLUGGING_BADLY.ogg'), 1),
        SoundRequest.NEEDS_PLUGGING_BADLY   : (os.path.join(sounds_dir, 'NEEDS_PLUGGING_BADLY.ogg'), 1),
    }

    def __init__(self):
        super(SoundPlayNode, self).__init__()
        self.tmpdir = self.make_temp_dir()
        self.sounds = list()
        self.lock = Lock()
        self.device = rospy.get_param("~device", str())

        self.server = actionlib.SimpleActionServer(
            "sound_play", SoundRequestAction,
            execute_cb=self.execute_cb,
            auto_start=False)

        self.sub = rospy.Subscriber("robotsound", SoundRequest, self.msg_cb)
        self.server.start()
        self.timer = rospy.Timer(rospy.Duration(0.5), self.timer_cb)

    def make_temp_dir(self):
        seed = string.ascii_letters + string.digits
        while not rospy.is_shutdown():
            suf = "_" + str().join([random.choice(seed) for i in range(6)])
            dirname = (rospy.get_name() + suf).split("/")[-1]
            tmpdir = os.path.join(tempfile.gettempdir(), dirname)
            if not os.path.exists(tmpdir):
                break
        os.makedirs(tmpdir)
        rospy.loginfo("Created %s" % tmpdir)
        return tmpdir

    def make_uri(self, msg):
        if msg.sound == SoundRequest.PLAY_FILE:
            if msg.arg2:
                uri = os.path.join(PKG.get_path(msg.arg2), msg.arg)
            else:
                uri = msg.arg
        elif msg.sound == SoundRequest.SAY:
            basename = msg.arg + "_" + msg.arg2
            basename = re.sub(r',|\.|/|\\| |\'|`|"|;|:|\(|\)|%|~', '_', basename)
            uri = os.path.join(self.tmpdir, basename + ".wav")
        else:
            uri, _ = SoundPlayNode.builtin_sounds[msg.sound]

        return uri

    def make_speech_wav(self, sentence, param, wavpath):
        if os.path.isfile(wavpath) and os.stat(wavpath).st_size > 0:
            return True
        txtpath = os.path.splitext(wavpath)[0] + '.txt'
        with open(txtpath, "w") as f:
            f.write(sentence)
        cmd = ['text2wave', '-eval', '(' + param + ')', txtpath, '-o', wavpath]
        retcode = subprocess.check_call(cmd)
        rospy.logdebug('created wav file with command %s (return code: %d)' % (cmd, retcode))
        if retcode != 0 or os.stat(wavpath).st_size == 0:
            try:
                os.remove(wavpath)
            except:
                pass
            rospy.logerr("Failed: text-to-speech (%d)" % retcode)
            return False
        else:
            return True

    def process_msg(self, msg):
        # stop
        if msg.command == SoundRequest.PLAY_STOP:
            for sound in self.sounds:
                if msg.sound == SoundRequest.ALL or self.make_uri(msg) == sound.uri:
                    sound.stop()
            return

        # play
        sound = Sound(device=self.device)
        uri = self.make_uri(msg)

        if msg.sound == SoundRequest.SAY:
            ok = self.make_speech_wav(msg.arg, msg.arg2, uri)
            if not ok:
                raise RuntimeError("Failed on text-to-speech")

        if msg.command == SoundRequest.PLAY_START:
            sound.loop = True

        if hasattr(msg, "volume"):
            volume = getattr(msg, "volume")
            if volume > 0:
                sound.volume = volume

        sound.uri = uri
        sound.play()
        self.sounds.append(sound)
        return sound

    def msg_cb(self, msg):
        try:
            self.process_msg(msg)
        except Exception as e:
            rospy.logerr(e)
            rospy.logerr(traceback.format_exc())

    def execute_cb(self, goal):
        try:
            msg = goal.sound_request
            sound = self.process_msg(msg)
        except Exception as e:
            rospy.logerr(e)
            rospy.logerr(traceback.format_exc())
            self.server.set_aborted()
            return

        start = rospy.Time.now()
        feedback = SoundRequestFeedback()

        r = rospy.Rate(10)
        while True:
            r.sleep()

            if rospy.is_shutdown():
                self.server.set_aborted()
                return
            elif sound.status != Gst.State.PLAYING:
                break
            elif self.server.is_preempt_requested():
                sound.stop()
                self.server.set_preempted()
                return

            feedback.playing = True
            feedback.stamp = rospy.Time.now() - start
            self.server.publish_feedback(feedback)

        result = SoundRequestResult()
        result.playing = False
        result.stamp = rospy.Time.now() - start
        if sound.ret is not None:
            if sound.ret == Gst.StateChangeReturn.FAILURE:
                self.server.set_aborted()
            else:
                self.server.set_succeeded(result)
        else:
            self.server.set_aborted()

    def timer_cb(self, event):
        remove = list()
        sounds = list()
        for sound in self.sounds:
            sound.poll()
            if sound.status == Gst.State.NULL or sound.status is None:
                rospy.logdebug("Del: %s" % sound.uri)
                sound.dispose()
                del sound
                rospy.logdebug("GC: %d" % gc.collect())
            else:
                sounds.append(sound)
        self.sounds = sounds


if __name__ == '__main__':
    rospy.init_node("sound_play_node")
    n = SoundPlayNode()
    rospy.spin()
