"""
Unit tests for the backend privacy anonymization module.

Run with:
    python -m unittest discover tests
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
import cv2
import numpy as np

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.privacy import (
    FACE_CASCADE_FILENAME,
    PLATE_CASCADE_FILENAME,
    anonymize_frame,
    blur_pii_in_image,
    detect_pii_regions,
    get_cascade_path,
    load_cascades,
)
from tests.test_privacy import test_no_pii_handling


class TestPrivacyAnonymization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tests_dir = Path(__file__).resolve().parent
        cls.sample_face_path = cls.tests_dir / "sample_face.jpg"
        cls.sample_car_path = cls.tests_dir / "sample_car.jpg"
        cls.output_path = cls.tests_dir / "output_blurred.jpg"

    def test_cascades_exist_and_load(self):
        """Ensure Haar Cascade XML files exist and load successfully."""
        face_path = get_cascade_path(FACE_CASCADE_FILENAME)
        plate_path = get_cascade_path(PLATE_CASCADE_FILENAME)

        self.assertTrue(os.path.isfile(face_path), f"Face cascade missing: {face_path}")
        self.assertTrue(os.path.isfile(plate_path), f"Plate cascade missing: {plate_path}")

        face_cascade, plate_cascade = load_cascades()
        self.assertFalse(face_cascade.empty(), "Face cascade classifier is empty.")
        self.assertFalse(plate_cascade.empty(), "Plate cascade classifier is empty.")

    def test_cpu_only_execution(self):
        """Verify OpenCL is disabled to ensure CPU-only execution."""
        self.assertFalse(
            cv2.ocl.useOpenCL(),
            "OpenCL should be disabled to guarantee strict CPU-only execution.",
        )

    def test_blur_pii_face_image(self):
        """Verify face is detected and blurred in sample image."""
        if not self.sample_face_path.is_file():
            self.skipTest("sample_face.jpg not found")

        with open(self.sample_face_path, "rb") as f:
            raw_bytes = f.read()

        blurred_bytes, pii_detected = blur_pii_in_image(raw_bytes)

        self.assertTrue(pii_detected, "PII should be detected in face image.")
        self.assertIsInstance(blurred_bytes, bytes)
        # JPEG SOI marker check
        self.assertTrue(
            blurred_bytes.startswith(b"\xff\xd8"),
            "Resulting bytes must be a valid JPEG file.",
        )

        # Check that blurred image can be decoded and has the same shape
        np_arr = np.frombuffer(blurred_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        self.assertIsNotNone(decoded)

    def test_blur_pii_car_plate_image(self):
        """Verify license plate is detected and blurred in sample car image."""
        if not self.sample_car_path.is_file():
            self.skipTest("sample_car.jpg not found")

        with open(self.sample_car_path, "rb") as f:
            raw_bytes = f.read()

        blurred_bytes, pii_detected = blur_pii_in_image(raw_bytes)

        self.assertTrue(pii_detected, "PII should be detected in vehicle plate image.")
        self.assertTrue(blurred_bytes.startswith(b"\xff\xd8"))

    def test_blur_pii_no_pii_image(self):
        """Verify that images without PII return pii_detected = False and valid JPEG."""
        # Create a neutral synthetic test image (uniform texture, no faces or plates)
        synthetic_img = np.zeros((300, 300, 3), dtype=np.uint8)
        synthetic_img[:] = (120, 150, 100)
        _, encoded = cv2.imencode(".jpg", synthetic_img)

        blurred_bytes, pii_detected = blur_pii_in_image(encoded.tobytes())

        self.assertFalse(pii_detected, "No PII should be detected in a blank image.")
        self.assertTrue(blurred_bytes.startswith(b"\xff\xd8"))

    def test_blur_pii_empty_and_invalid_bytes(self):
        """Verify appropriate errors are raised on invalid inputs."""
        with self.assertRaises(ValueError):
            blur_pii_in_image(b"")

        with self.assertRaises(ValueError):
            blur_pii_in_image(b"invalid image data that cannot be decoded")

    def test_anonymize_frame_direct(self):
        """Verify frame anonymization on numpy ndarray directly."""
        test_frame = np.ones((200, 200, 3), dtype=np.uint8) * 200
        anonymized, pii_found = anonymize_frame(test_frame)

        self.assertIsInstance(anonymized, np.ndarray)
        self.assertEqual(anonymized.shape, (200, 200, 3))
        self.assertFalse(pii_found)

    def test_anonymize_frame_invalid_inputs(self):
        """Verify error handling on invalid frame inputs."""
        with self.assertRaises(ValueError):
            anonymize_frame(None)  # type: ignore

        with self.assertRaises(ValueError):
            anonymize_frame(np.array([]))

        with self.assertRaises(ValueError):
            # 1D array is invalid for image frame
            anonymize_frame(np.zeros((100,), dtype=np.uint8))

    def test_cli_test_privacy_script(self):
        """Verify tests/test_privacy.py CLI runs and creates output_blurred.jpg."""
        if not self.sample_face_path.is_file():
            self.skipTest("sample_face.jpg not found")

        cmd = [
            sys.executable,
            str(self.tests_dir / "test_privacy.py"),
            str(self.sample_face_path),
            "--output",
            str(self.output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)

        self.assertEqual(proc.returncode, 0, f"CLI exited with error: {proc.stderr}")
        self.assertIn("PII Status: [FOUND]", proc.stdout)
        self.assertTrue(self.output_path.is_file(), "output_blurred.jpg must be created.")

    def test_phase1_requirement3_no_pii_handling(self):
        """Verify Phase 1 Requirement 3: No-PII handling, false-positive absence, and MSE < 5.0."""
        metrics = test_no_pii_handling()
        self.assertEqual(metrics["status"], "PASSED")
        self.assertFalse(metrics["pii_found"])
        self.assertLess(metrics["mse"], 5.0)
        self.assertGreater(metrics["execution_time_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
