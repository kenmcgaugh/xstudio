from xstudio.api.session.playlist.timeline import Timeline, Track, Clip
from xstudio.core import UuidActor, Uuid, actor, item_atom, link_media_atom
from xstudio.core import FrameList, FrameRange, FrameRateDuration

# We have to use a loopback connection to xstudio to avoid
# the "Failed to spawn remote actor" error.
from xstudio.connection import Connection as _Connection
loopback_connection = _Connection(auto_connect=True)

def uniquify_clip_media(timeline=loopback_connection.api.session.viewed_container):
    if not isinstance(timeline, Timeline):
        return

    playlist = timeline.parent_playlist

    for item in timeline.selection:
        if isinstance(item, Track):
            new_track = timeline.insert_video_track(item.item_name+" Uniqued")
            for i, clip in enumerate(item.clips):
                mr = clip.media.media_source().media_reference
                new_media = playlist.add_media(mr.uri())

                # rename both the clip and media
                name = f"Shot {i+1:03d}"
                clip.item_name = name
                new_media.name = name

                # adjust the media's timing info to trim it
                # to the clip's range
                new_start = clip.trimmed_range.frame_start().frames()
                new_duration = clip.trimmed_range.frame_duration().frames()
                new_mr = new_media.media_source().media_reference
                new_mr.set_offset(new_start)
                new_mr.set_duration(FrameRateDuration(new_duration, new_mr.rate()))
                new_media.media_source().media_reference = new_mr

                timeline.add_media(new_media)
                new_track.insert_clip(new_media)
        elif isinstance(item, Clip):
            new_track = timeline.insert_video_track(item.item_name+" Uniqued")


uniquify_clip_media()
