"""Media files: photos, videos and the extensions the pipeline handles."""

from ouestcharlie_toolkit.media.photo import Photo
from ouestcharlie_toolkit.media.suffixes import PHOTO_SUFFIXES, VIDEO_SUFFIXES
from ouestcharlie_toolkit.media.video import Video, video_identity_hash

__all__ = ["PHOTO_SUFFIXES", "VIDEO_SUFFIXES", "Photo", "Video", "video_identity_hash"]
