"""Local in-process inference providers."""

from .audio_event import (
    AST_AUDIOSET_MAPPING_VERSION,
    DEFAULT_AUDIO_EVENT_BATCH_SIZE,
    DEFAULT_AUDIO_EVENT_DEVICE,
    DEFAULT_AUDIO_EVENT_MODEL,
    LocalAudioEventProvider,
    build_audioset_label_mapping,
    create_local_audio_event_service,
)
from .caption import LocalCaptionProvider, create_local_caption_service
from .diarization import (
    LocalDiarizationProvider,
    create_local_diarization_service,
)
from .embedding import LocalEmbeddingProvider, get_local_embedding_service
from .ocr import LocalOCRProvider, create_local_ocr_service
from .stt import LocalSTTProvider, create_local_stt_service
from .vad import LocalVADProvider, create_local_vad_service

__all__ = [
    "AST_AUDIOSET_MAPPING_VERSION",
    "DEFAULT_AUDIO_EVENT_BATCH_SIZE",
    "DEFAULT_AUDIO_EVENT_DEVICE",
    "DEFAULT_AUDIO_EVENT_MODEL",
    "LocalAudioEventProvider",
    "LocalCaptionProvider",
    "LocalDiarizationProvider",
    "LocalEmbeddingProvider",
    "LocalOCRProvider",
    "LocalSTTProvider",
    "LocalVADProvider",
    "build_audioset_label_mapping",
    "create_local_audio_event_service",
    "create_local_caption_service",
    "create_local_diarization_service",
    "create_local_ocr_service",
    "create_local_stt_service",
    "create_local_vad_service",
    "get_local_embedding_service",
]
