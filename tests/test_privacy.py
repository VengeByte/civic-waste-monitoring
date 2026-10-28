"""
CLI test script for image privacy anonymization module.

Reads an image file containing a face or vehicle, processes it through
`blur_pii_in_image`, prints whether PII was found, and saves the resulting
blurred image to `tests/output_blurred.jpg` (or a custom output path).

Also includes `test_no_pii_handling()` to verify that images with no PII are not
modified and do not trigger false positives, computing MSE and execution metrics.

Usage:
    # Run the No-PII handling test & metric report:
    python tests/test_privacy.py

    # Or run on a specific image containing a face or vehicle:
    python tests/test_privacy.py <path_to_image> [--output <output_path>]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict

import cv2
import numpy as np

# Add project root to sys.path to allow imports from backend
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.privacy import blur_pii_in_image


def generate_synthetic_pavement_image(width: int = 512, height: int = 512) -> bytes:
    """
    Programmatically generate a synthetic asphalt/pavement textured image with
    realistic granular noise and a subtle lighting gradient, containing zero
    human faces or vehicle license plates, encoded as JPEG bytes (quality 85).

    Args:
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        Raw JPEG bytes of the synthetic pavement image.
    """
    rng = np.random.default_rng(seed=42)

    # Base dark gray asphalt tone
    base_color = 75.0

    # Subtle spatial lighting gradients across pavement surface
    y_grad = np.linspace(-8.0, 8.0, height, dtype=np.float64).reshape(height, 1, 1)
    x_grad = np.linspace(-5.0, 5.0, width, dtype=np.float64).reshape(1, width, 1)

    # Granular asphalt noise texture
    noise = rng.normal(0.0, 10.0, size=(height, width, 3))

    pavement = np.clip(base_color + y_grad + x_grad + noise, 0.0, 255.0).astype(np.uint8)

    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 85]
    success, buffer = cv2.imencode(".jpg", pavement, encode_params)
    if not success or buffer is None:
        raise RuntimeError("Failed to encode synthetic pavement image to JPEG.")

    return buffer.tobytes()


def test_no_pii_handling(sample_image_path: str | Path | None = None) -> dict[str, Any]:
    """
    Verify Phase 1 Requirement 3: Ensure images with no PII are not modified
    and do not trigger false positives.

    Performs the following steps:
    1. Loads a sample no-PII image if available, or programmatically generates a
       synthetic asphalt/pavement textured image and encodes it to JPEG bytes.
    2. Passes the bytes through blur_pii_in_image(original_bytes).
    3. Asserts that the returned flag pii_found == False (raises AssertionError
       if True, indicating a false positive).
    4. Decodes both input and output JPEG bytes back into NumPy arrays and
       calculates the Mean Squared Error (MSE):
           MSE = 1 / (H * W * C) * sum((I_orig - I_proc) ** 2)
    5. Asserts that MSE < 5.0 ensuring no blur kernel or alterations were applied
       to the pixels (accounting only for minor JPEG compression artifacts).
    6. Prints clear pass/fail status metrics (MSE score, PII flag, and execution time).

    Returns:
        dict containing 'status', 'pii_found', 'mse', and 'execution_time_ms'.
    """
    # 1. Determine image source: custom path, default file, or synthetic generation
    original_bytes: bytes
    image_desc: str

    candidate_paths = []
    if sample_image_path:
        candidate_paths.append(Path(sample_image_path))
    candidate_paths.extend([
        PROJECT_ROOT / "tests" / "sample_no_pii.jpg",
        PROJECT_ROOT / "tests" / "sample_asphalt.jpg",
    ])

    found_path = next((p for p in candidate_paths if p.is_file()), None)

    if found_path is not None:
        with open(found_path, "rb") as f:
            original_bytes = f.read()
        image_desc = f"File: {found_path.name}"
    else:
        original_bytes = generate_synthetic_pavement_image(width=512, height=512)
        image_desc = "Programmatically generated synthetic asphalt/pavement texture (512x512)"

    # 2. Pass bytes through blur_pii_in_image and measure execution time
    start_time = time.perf_counter()
    output_bytes, pii_found = blur_pii_in_image(original_bytes)
    execution_time_ms = (time.perf_counter() - start_time) * 1000.0

    # 3. Assert pii_found == False (raise AssertionError on false positive)
    if pii_found:
        fail_msg = (
            f"False positive detected: blur_pii_in_image returned pii_found=True "
            f"on a non-PII image ({image_desc})."
        )
        print(f"\n[FAILED] {fail_msg}", file=sys.stderr)
        raise AssertionError(fail_msg)

    # 4. Decode both original and output JPEG bytes back into NumPy image arrays
    orig_np = np.frombuffer(original_bytes, dtype=np.uint8)
    proc_np = np.frombuffer(output_bytes, dtype=np.uint8)

    i_orig = cv2.imdecode(orig_np, cv2.IMREAD_COLOR)
    i_proc = cv2.imdecode(proc_np, cv2.IMREAD_COLOR)

    if i_orig is None or i_proc is None:
        raise ValueError("Failed to decode original or processed image bytes for MSE computation.")

    if i_orig.shape != i_proc.shape:
        raise AssertionError(
            f"Shape mismatch: original shape {i_orig.shape} != processed shape {i_proc.shape}"
        )

    # Calculate Mean Squared Error (MSE): 1 / (H * W * C) * sum((I_orig - I_proc) ** 2)
    diff = i_orig.astype(np.float64) - i_proc.astype(np.float64)
    mse = float(np.mean(diff ** 2))

    # 5. Assert MSE < 5.0
    mse_threshold = 5.0
    if mse >= mse_threshold:
        fail_msg = (
            f"MSE threshold exceeded: MSE = {mse:.6f} >= {mse_threshold:.1f}. "
            f"Unexpected pixel alterations or blur kernel were applied to non-PII image."
        )
        print(f"\n[FAILED] {fail_msg}", file=sys.stderr)
        raise AssertionError(fail_msg)

    # 6. Print clear pass/fail status metrics to console
    print("\n" + "=" * 70)
    print("      PHASE 1 REQUIREMENT 3: NO-PII HANDLING VERIFICATION METRICS")
    print("=" * 70)
    print(f" Image Source       : {image_desc}")
    print(f" PII Flag Detected  : {pii_found} (Expected: False)")
    print(f" Execution Time     : {execution_time_ms:.2f} ms")
    print(f" Mean Squared Error : {mse:.6f} (Threshold: < {mse_threshold:.1f})")
    print(f" Pass/Fail Status   : [PASSED] - No false positives, pixels preserved")
    print("=" * 70 + "\n")

    return {
        "status": "PASSED",
        "pii_found": pii_found,
        "mse": mse,
        "execution_time_ms": execution_time_ms,
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CLI test script for PII anonymization and No-PII validation in civic images."
    )
    parser.add_argument(
        "image_path",
        type=str,
        nargs="?",
        default=None,
        help="Optional path to an input image containing a face or vehicle. If omitted, runs test_no_pii_handling().",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="tests/output_blurred.jpg",
        help="Path where the anonymized output JPEG will be saved (default: tests/output_blurred.jpg).",
    )
    parser.add_argument(
        "--test-no-pii",
        action="store_true",
        help="Run the test_no_pii_handling() false-positive and MSE verification test.",
    )
    return parser.parse_args()


def run_test(image_path_str: str, output_path_str: str) -> bool:
    input_path = Path(image_path_str)
    if not input_path.is_file():
        print(f"Error: Input image file '{image_path_str}' does not exist.", file=sys.stderr)
        return False

    print(f"Reading input image: {input_path.resolve()}")
    try:
        with open(input_path, "rb") as f:
            image_bytes = f.read()

        if not image_bytes:
            print("Error: Input image file is empty.", file=sys.stderr)
            return False

        blurred_bytes, pii_found = blur_pii_in_image(image_bytes)

        if pii_found:
            print("PII Status: [FOUND] - PII detected (face or license plate) and blurred.")
        else:
            print("PII Status: [NOT FOUND] - No face or license plate detected in image.")

        output_path = Path(output_path_str)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(blurred_bytes)

        print(f"Saved anonymized image to: {output_path.resolve()} ({len(blurred_bytes):,} bytes)")
        return True

    except Exception as exc:
        print(f"Error during privacy anonymization: {exc}", file=sys.stderr)
        return False


def main() -> int:
    args = parse_arguments()

    # If no image path is provided or --test-no-pii flag is explicitly set, run test_no_pii_handling()
    if args.image_path is None or args.test_no_pii:
        try:
            test_no_pii_handling(args.image_path if args.test_no_pii else None)
            if args.image_path is None:
                return 0
        except AssertionError:
            return 1

    if args.image_path is not None:
        success = run_test(args.image_path, args.output)
        return 0 if success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
