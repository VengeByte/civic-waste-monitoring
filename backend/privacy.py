"""
Privacy and Anonymization Module for Civic Garbage Reporting System.

This module provides CPU-based image and video frame anonymization for personally
identifiable information (PII), specifically human faces and vehicle license plates.
It leverages OpenCV's pre-trained Haar Cascades and NumPy to detect PII regions and
apply a heavy Gaussian blur (kernel size 51x51) directly over bounding boxes before
storing or transmitting civic reports.

Execution is strictly CPU-bound with no external network calls or GPU dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple
import cv2
import numpy as np

# Enforce strictly CPU execution by disabling OpenCL GPU offloading
cv2.ocl.setUseOpenCL(False)

FACE_CASCADE_FILENAME = "haarcascade_frontalface_default.xml"
PLATE_CASCADE_FILENAME = "haarcascade_russian_plate_number.xml"

# Cached CascadeClassifier instances
_face_cascade: cv2.CascadeClassifier | None = None
_plate_cascade: cv2.CascadeClassifier | None = None


def get_cascade_path(filename: str) -> str:
    """
    Resolve the filesystem path for a built-in OpenCV Haar Cascade XML file.

    Args:
        filename: Name of the Haar cascade XML file.

    Returns:
        Absolute filesystem path to the XML file.

    Raises:
        FileNotFoundError: If the cascade file cannot be located.
    """
    # 1. Primary location: OpenCV bundled data directory
    if hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
        cascade_path = os.path.join(cv2.data.haarcascades, filename)
        if os.path.isfile(cascade_path):
            return os.path.abspath(cascade_path)

    # 2. Fallback locations: local directory, package data, current working directory
    search_dirs = [
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent / "data",
        Path.cwd(),
    ]
    for directory in search_dirs:
        candidate = directory / filename
        if candidate.is_file():
            return str(candidate.resolve())

    raise FileNotFoundError(
        f"Haar cascade XML file '{filename}' could not be located in cv2.data.haarcascades "
        f"or standard fallback directories."
    )


def load_cascades() -> tuple[cv2.CascadeClassifier, cv2.CascadeClassifier]:
    """
    Load and cache Haar Cascade classifiers for face and license plate detection.

    Returns:
        Tuple of (face_cascade, plate_cascade).

    Raises:
        FileNotFoundError: If cascade XML files are missing.
        RuntimeError: If cascade classifiers fail to initialize.
    """
    global _face_cascade, _plate_cascade

    if _face_cascade is None or _face_cascade.empty():
        face_path = get_cascade_path(FACE_CASCADE_FILENAME)
        classifier = cv2.CascadeClassifier(face_path)
        if classifier.empty():
            raise RuntimeError(f"Failed to load Haar cascade from '{face_path}'.")
        _face_cascade = classifier

    if _plate_cascade is None or _plate_cascade.empty():
        plate_path = get_cascade_path(PLATE_CASCADE_FILENAME)
        classifier = cv2.CascadeClassifier(plate_path)
        if classifier.empty():
            raise RuntimeError(f"Failed to load Haar cascade from '{plate_path}'.")
        _plate_cascade = classifier

    return _face_cascade, _plate_cascade


def detect_pii_regions(
    gray_frame: np.ndarray,
    face_scale_factor: float = 1.1,
    face_min_neighbors: int = 5,
    face_min_size: tuple[int, int] = (30, 30),
    plate_scale_factor: float = 1.1,
    plate_min_neighbors: int = 4,
    plate_min_size: tuple[int, int] = (30, 15),
) -> list[tuple[int, int, int, int]]:
    """
    Detect bounding boxes for faces and license plates in a single grayscale frame.

    Args:
        gray_frame: Grayscale 2D numpy array representing the image.
        face_scale_factor: Scale factor parameter for face detection.
        face_min_neighbors: Minimum neighbors parameter for face detection.
        face_min_size: Minimum bounding box size for faces (width, height).
        plate_scale_factor: Scale factor parameter for plate detection.
        plate_min_neighbors: Minimum neighbors parameter for plate detection.
        plate_min_size: Minimum bounding box size for license plates.

    Returns:
        List of bounding box tuples: [(x, y, w, h), ...]
    """
    face_cascade, plate_cascade = load_cascades()
    boxes: list[tuple[int, int, int, int]] = []

    # Detect human faces
    faces = face_cascade.detectMultiScale(
        gray_frame,
        scaleFactor=face_scale_factor,
        minNeighbors=face_min_neighbors,
        minSize=face_min_size,
        flags=cv2.CASCADE_SCALE_IMAGE,
    )
    if len(faces) > 0:
        for (x, y, w, h) in faces:
            boxes.append((int(x), int(y), int(w), int(h)))

    # Detect vehicle license plates
    plates = plate_cascade.detectMultiScale(
        gray_frame,
        scaleFactor=plate_scale_factor,
        minNeighbors=plate_min_neighbors,
        minSize=plate_min_size,
        flags=cv2.CASCADE_SCALE_IMAGE,
    )
    if len(plates) > 0:
        for (x, y, w, h) in plates:
            boxes.append((int(x), int(y), int(w), int(h)))

    return boxes


def anonymize_frame(
    frame: np.ndarray,
    kernel_size: tuple[int, int] = (51, 51),
) -> tuple[np.ndarray, bool]:
    """
    Anonymize personally identifiable information (PII) in an OpenCV image or video frame.

    Runs multi-scale detection for faces and license plates on CPU and applies a
    heavy Gaussian blur directly over the detected bounding box regions.

    Args:
        frame: BGR or Grayscale numpy image array.
        kernel_size: Kernel dimensions for Gaussian blur (must be positive and odd).

    Returns:
        tuple[np.ndarray, bool]: (anonymized_frame, pii_detected)
    """
    if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
        raise ValueError("Invalid frame: input must be a non-empty numpy array.")

    output_frame = frame.copy()

    # Convert to grayscale for Haar cascade multi-scale detection
    if len(frame.shape) == 2:
        gray = frame
    elif len(frame.shape) == 3 and frame.shape[2] >= 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError(f"Unsupported frame dimensions: {frame.shape}")

    boxes = detect_pii_regions(gray)
    pii_detected = len(boxes) > 0

    frame_height, frame_width = output_frame.shape[:2]

    for (x, y, w, h) in boxes:
        # Clamp ROI bounds strictly inside image boundaries
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(frame_width, x + w)
        y2 = min(frame_height, y + h)

        if x2 <= x1 or y2 <= y1:
            continue

        roi = output_frame[y1:y2, x1:x2]
        blurred_roi = cv2.GaussianBlur(roi, kernel_size, 0)
        output_frame[y1:y2, x1:x2] = blurred_roi

    return output_frame, pii_detected


def blur_pii_in_image(image_bytes: bytes) -> tuple[bytes, bool]:
    """
    Decode an image from raw bytes, detect PII (faces and license plates), apply
    a heavy Gaussian blur (kernel size 51x51) directly over the bounding boxes,
    and re-encode the image to JPEG bytes (quality 85).

    Args:
        image_bytes: Raw image bytes (JPEG, PNG, WebP, etc.).

    Returns:
        tuple[bytes, bool]:
            - bytes: Re-encoded JPEG image bytes with blurred PII regions (quality 85).
            - bool: True if PII (face or license plate) was detected and blurred, False otherwise.

    Raises:
        ValueError: If image_bytes is empty or cannot be decoded.
        RuntimeError: If JPEG re-encoding fails.
    """
    if not image_bytes or not isinstance(image_bytes, (bytes, bytearray)):
        raise ValueError("image_bytes must be non-empty bytes.")

    np_buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(np_buffer, cv2.IMREAD_COLOR)

    if image is None:
        raise ValueError("Failed to decode image from provided bytes.")

    blurred_image, pii_detected = anonymize_frame(image, kernel_size=(51, 51))

    # Re-encode to JPEG bytes with quality 85
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 85]
    success, encoded_buffer = cv2.imencode(".jpg", blurred_image, encode_params)

    if not success or encoded_buffer is None:
        raise RuntimeError("Failed to re-encode anonymized image to JPEG format.")

    return encoded_buffer.tobytes(), pii_detected
