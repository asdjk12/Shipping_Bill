from .interface import OCRBackend, OCRResult, run_ocr
from .local import LocalOCR
from .paddle import PaddleStructure

__all__ = ["LocalOCR", "OCRBackend", "OCRResult", "PaddleStructure", "run_ocr"]
