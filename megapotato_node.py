"""MegaPotato AI upscaler (https://megapotato.app) as a ComfyUI node.

IMAGE in, IMAGE out at x1 / x2 / x4 / x8 through the public MegaPotato API
(https://megapotato.app/developers): the photo is uploaded, processed on MegaPotato's GPUs and
the finished PNG is downloaded back into the workflow. Needs an API key from
https://megapotato.app/account.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
import torch
from PIL import Image

try:  # ComfyUI runtime pieces; absent when the module is imported outside ComfyUI
    import comfy.utils as comfy_utils
except Exception:  # pragma: no cover
    comfy_utils = None
try:
    import comfy.model_management as comfy_mm
except Exception:  # pragma: no cover
    comfy_mm = None
try:
    from server import PromptServer
except Exception:  # pragma: no cover
    PromptServer = None

__version__ = "0.1.1"

log = logging.getLogger("comfyui-megapotato")

DEFAULT_API_BASE = "https://megapotato.app"
ENV_API_KEY = "MEGAPOTATO_API_KEY"
ENV_API_BASE = "MEGAPOTATO_API_BASE"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "megapotato_api_key.txt")
USER_AGENT = f"comfyui-megapotato/{__version__}"

# Documented API limits (https://megapotato.app/developers), checked locally so an oversized
# request fails with a readable message before anything is uploaded.
MAX_INPUT_EDGE = 6000
MAX_INPUT_PIXELS = 20_250_000
MAX_OUTPUT_EDGE = 16_400
MAX_OUTPUT_PIXELS = 268_960_000
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

WAIT_SECONDS = 55  # the API's long-poll ceiling per GET /v1/jobs/{id}
MAX_PARALLEL_JOBS = 4  # per node run; the account allows 10 queued/running jobs
UPSCALE_CHOICES = ["1", "2", "4", "8"]


class MegaPotatoApiError(RuntimeError):
    """An API answer the node cannot recover from; the message is meant for the user."""


# ----------------------------------------------------------------------------- key + checks


def resolve_api_key(override: str) -> str:
    """The key to use: the node's own field, then the environment, then the key file."""
    key = (override or "").strip()
    if not key:
        key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        try:
            with open(KEY_FILE, encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError:
            key = ""
    if not key:
        raise ValueError(
            "No MegaPotato API key. Create one at https://megapotato.app/account, then set the "
            f"{ENV_API_KEY} environment variable (or save it in megapotato_api_key.txt next to this "
            "node, or paste it into the api_key_override field)."
        )
    return key


def check_dimensions(width: int, height: int, factor: int) -> None:
    """Raise a readable ValueError when the input or the output would exceed the API limits."""
    if max(width, height) > MAX_INPUT_EDGE or width * height > MAX_INPUT_PIXELS:
        raise ValueError(
            f"MegaPotato: the input is {width}x{height}; the limit is {MAX_INPUT_EDGE} px per side "
            f"and {MAX_INPUT_PIXELS / 1e6:.2f} MP. Downscale the image first."
        )
    out_w, out_h = width * factor, height * factor
    if max(out_w, out_h) > MAX_OUTPUT_EDGE or out_w * out_h > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"MegaPotato: x{factor} of {width}x{height} would be {out_w}x{out_h}, over the output "
            f"limit of {MAX_OUTPUT_EDGE} px per side / {MAX_OUTPUT_PIXELS / 1e6:.0f} MP. "
            f"x{factor} needs an input of at most {MAX_OUTPUT_EDGE // factor} px per side; "
            "pick a smaller factor or a smaller input."
        )


# ----------------------------------------------------------------------------- image <-> bytes


def tensor_to_upload(image: torch.Tensor) -> tuple[bytes, str, str]:
    """One ComfyUI image [H, W, C] (float 0..1) -> (bytes, filename, mime) for the upload.

    PNG first; a photo whose PNG is over the 40 MiB cap is retried as lossless WebP. Alpha is
    composited onto white (the API does the same), so the preview in the workflow matches the result.
    """
    arr = image.detach().cpu().float().clamp(0.0, 1.0).numpy()
    if arr.ndim == 2:
        arr = arr[..., None]
    arr = (arr * 255.0 + 0.5).astype(np.uint8)
    channels = arr.shape[-1]
    if channels == 1:
        pil = Image.fromarray(arr[..., 0], "L").convert("RGB")
    elif channels == 4:
        rgba = Image.fromarray(arr, "RGBA")
        matte = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        pil = Image.alpha_composite(matte, rgba).convert("RGB")
    else:
        pil = Image.fromarray(np.ascontiguousarray(arr[..., :3]), "RGB")

    buf = io.BytesIO()
    pil.save(buf, format="PNG", compress_level=6)
    data = buf.getvalue()
    if len(data) <= MAX_UPLOAD_BYTES:
        return data, "image.png", "image/png"
    buf = io.BytesIO()
    pil.save(buf, format="WEBP", lossless=True, quality=100)
    data = buf.getvalue()
    if len(data) <= MAX_UPLOAD_BYTES:
        return data, "image.webp", "image/webp"
    raise ValueError(
        f"MegaPotato: the encoded image is {len(data) / 2**20:.0f} MiB, over the 40 MiB upload cap. "
        "Downscale the image first."
    )


def bytes_to_tensor(data: bytes) -> torch.Tensor:
    """A downloaded PNG -> ComfyUI image tensor [1, H, W, 3] (float32 0..1)."""
    # The result can be up to 269 MP, far above Pillow's decompression-bomb guard; the bytes
    # come from the URL the API just handed us, so lift the guard for this decode only.
    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        pil = Image.open(io.BytesIO(data))
        pil.load()
        pil = pil.convert("RGB")
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit
    arr = np.asarray(pil, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


# ----------------------------------------------------------------------------- API client


def _api_error(resp: requests.Response, context: str) -> MegaPotatoApiError:
    """Turn an error envelope ({"error": {"type", "message"}}) into a message with a next step."""
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else None
    etype = (err or {}).get("type", "") if isinstance(err, dict) else ""
    message = (err or {}).get("message", "") if isinstance(err, dict) else ""
    message = message or resp.text[:300] or f"HTTP {resp.status_code}"
    hints = {
        "authentication_error": "Check the API key (https://megapotato.app/account).",
        "insufficient_credits": "Top up chips at https://megapotato.app/pricing.",
        "spend_limit_reached": "The account's daily API spending limit is reached; it resets at "
        "00:00 UTC and can be changed at https://megapotato.app/account.",
    }
    hint = hints.get(etype, "")
    return MegaPotatoApiError(
        f"MegaPotato {context} failed ({resp.status_code} {etype or 'error'}): {message}"
        + (f" {hint}" if hint else "")
    )


def _retry_after(resp: requests.Response, default: float) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", default)), 120.0)
    except ValueError:
        return default


class MegaPotatoClient:
    """Thin wrapper over the three /v1 calls with the retry rules from the API docs."""

    def __init__(self, api_key: str, base_url: str, timeout_seconds: float):
        self.base = base_url.rstrip("/")
        self.deadline = time.monotonic() + timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT})

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    def quote(self, width: int, height: int, factor: int) -> dict:
        resp = self.session.post(
            f"{self.base}/v1/quote",
            json={"width": width, "height": height, "upscale": factor},
            timeout=(30, 60),
        )
        if not resp.ok:
            raise _api_error(resp, "quote")
        return resp.json()

    def submit(self, data: bytes, filename: str, mime: str, factor: int, idem_key: str, stop: threading.Event) -> dict:
        """POST /v1/upscale. Retries 5xx / connection errors / rate limits with the SAME
        Idempotency-Key, so a retried upload can never be charged twice."""
        attempt = 0
        while True:
            attempt += 1
            if stop.is_set():
                raise MegaPotatoApiError("MegaPotato: cancelled")
            try:
                resp = self.session.post(
                    f"{self.base}/v1/upscale",
                    files={"file": (filename, data, mime)},
                    data={"upscale": str(factor)},
                    headers={"Idempotency-Key": idem_key},
                    timeout=(30, 300),
                )
            except requests.RequestException as exc:
                if attempt >= 4 or self._remaining() < 10:
                    raise MegaPotatoApiError(f"MegaPotato upload failed: {exc}") from exc
                time.sleep(2.0 * attempt)
                continue
            if resp.ok:
                return resp.json()
            if resp.status_code == 429:
                etype = ""
                try:
                    etype = resp.json()["error"]["type"]
                except (ValueError, KeyError, TypeError):
                    pass
                if etype == "rate_limit_error":
                    delay = _retry_after(resp, 15.0)
                    if self._remaining() < delay + 10:
                        raise _api_error(resp, "upload")
                    log.info("MegaPotato: rate limited, retrying the upload in %.0f s", delay)
                    time.sleep(delay)
                    continue
                raise _api_error(resp, "upload")
            if resp.status_code >= 500 and attempt < 4 and self._remaining() > 10:
                time.sleep(2.0 * attempt)
                continue
            raise _api_error(resp, "upload")

    def poll(self, job_id: str, wait: int, stop: threading.Event) -> dict:
        """GET /v1/jobs/{id}?wait=N. Polls are safe to retry, so transient faults just loop."""
        while True:
            if stop.is_set():
                raise MegaPotatoApiError("MegaPotato: cancelled")
            try:
                resp = self.session.get(
                    f"{self.base}/v1/jobs/{job_id}", params={"wait": wait}, timeout=(30, wait + 30)
                )
            except requests.RequestException as exc:
                if self._remaining() < 10:
                    raise MegaPotatoApiError(f"MegaPotato poll failed: {exc}") from exc
                time.sleep(3.0)
                continue
            if resp.ok:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                if self._remaining() < 10:
                    raise _api_error(resp, "poll")
                time.sleep(_retry_after(resp, 5.0))
                continue
            raise _api_error(resp, "poll")

    @staticmethod
    def download(url: str) -> bytes:
        # The result URL needs no Authorization header (it expires after about an hour).
        resp = requests.get(url, timeout=(30, 600), headers={"User-Agent": USER_AGENT})
        if not resp.ok:
            raise MegaPotatoApiError(f"MegaPotato result download failed: HTTP {resp.status_code}")
        return resp.content


# ----------------------------------------------------------------------------- progress


class _Progress:
    """Bar + status line on the node; both are optional (older ComfyUI)."""

    def __init__(self, total: int, node_id):
        self.node_id = node_id
        self.bar = None
        self._last_text = None
        if comfy_utils is not None:
            try:
                self.bar = comfy_utils.ProgressBar(total, node_id=node_id)
            except TypeError:  # older ComfyUI: ProgressBar(total)
                self.bar = comfy_utils.ProgressBar(total)

    def value(self, value: float) -> None:
        if self.bar is not None:
            self.bar.update_absolute(int(value))

    def text(self, text: str) -> None:
        if text == self._last_text:
            return
        self._last_text = text
        log.info("MegaPotato: %s", text)
        server = getattr(PromptServer, "instance", None) if PromptServer is not None else None
        if server is not None and self.node_id is not None and hasattr(server, "send_progress_text"):
            try:
                server.send_progress_text(text, self.node_id)
            except Exception:  # a UI nicety; never fail the job over it
                pass


def _fraction(entry: dict, now: float) -> float:
    phase = entry.get("phase")
    if phase == "done":
        return 1.0
    if phase in (None, "encoding"):
        return 0.0
    if phase == "uploading":
        return 0.05
    elapsed = max(0.0, now - entry.get("started", now))
    eta = entry.get("eta")
    if not eta or eta <= 0:
        eta = 90.0
    return 0.05 + 0.9 * (elapsed / (elapsed + eta))


def _status_line(state: dict, count: int) -> str:
    parts = []
    for i in range(count):
        entry = state.get(i) or {}
        phase = entry.get("phase") or "waiting"
        label = f"job {i + 1}/{count} " if count > 1 else ""
        if phase in ("queued", "running"):
            eta = entry.get("eta")
            if eta:
                parts.append(f"{label}{phase}, ~{int(eta)} s left")
            elif phase == "queued":
                parts.append(f"{label}queued (can take an extra 2-3 min while capacity comes online)")
            else:
                parts.append(f"{label}running")
        else:
            parts.append(f"{label}{phase}")
    return " | ".join(parts)


# ----------------------------------------------------------------------------- one job


def run_one(client: MegaPotatoClient, image: torch.Tensor, factor: int, rerun: int, state: dict, index: int, stop: threading.Event) -> tuple[str, int, bytes]:
    """Encode -> submit -> poll -> download for one image. Returns (job_id, cost_chips, png bytes)."""
    state[index] = {"phase": "encoding"}
    data, filename, mime = tensor_to_upload(image)
    # Same photo + same factor (+ same rerun counter) -> same key -> the API returns the earlier
    # job instead of charging again, for as long as results are kept (90 days).
    idem_key = "comfyui-" + hashlib.sha256(data + f"|{factor}|{rerun}".encode()).hexdigest()[:40]
    state[index] = {"phase": "uploading", "started": time.monotonic()}
    submitted = client.submit(data, filename, mime, factor, idem_key, stop)
    job_id = str(submitted.get("id"))
    cost = int(submitted.get("cost_chips") or 0)
    state[index] = {"phase": "queued", "started": time.monotonic(), "job_id": job_id, "cost": cost, "eta": None}

    mint_attempts = 0
    while True:
        if client._remaining() <= 0:
            raise MegaPotatoApiError(
                f"MegaPotato job {job_id} did not finish within the timeout. It keeps running on the "
                "server: run the node again with the same image and factor to pick up the result "
                "without paying twice."
            )
        job = client.poll(job_id, int(max(1, min(WAIT_SECONDS, client._remaining()))), stop)
        status = job.get("status")
        state[index].update(phase=status, eta=job.get("eta_seconds"))
        if status == "completed":
            url = job.get("result_url")
            if not url:
                mint_attempts += 1
                if mint_attempts > 3:
                    raise MegaPotatoApiError(f"MegaPotato job {job_id} completed but no result URL was issued.")
                time.sleep(1.0)
                continue
            state[index]["phase"] = "downloading"
            data = client.download(url)
            state[index]["phase"] = "done"
            return job_id, cost, data
        if status == "failed":
            raise MegaPotatoApiError(f"MegaPotato job {job_id} failed: {job.get('error') or 'processing failed'}")
        # queued / running / anything unknown: keep polling


# ----------------------------------------------------------------------------- the node


class MegaPotatoUpscale:
    """Upscale images x1-x8 with the MegaPotato AI upscaler (megapotato.app) over its API."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "The photo(s) to upscale. A batch becomes one API job per image."}),
                "upscale": (
                    UPSCALE_CHOICES,
                    {
                        "default": "4",
                        "tooltip": "Scale factor. 1 enhances without resizing. Limits: input up to "
                        "6000 px per side; output up to 16 400 px per side (x8 needs an input of "
                        "2050 px or less). An x8 result can be a multi-GB tensor in RAM.",
                    },
                ),
            },
            "optional": {
                "api_key_override": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Leave empty and set the MEGAPOTATO_API_KEY environment variable "
                        "instead: a key typed here is saved into the workflow JSON and PNG metadata.",
                    },
                ),
                "timeout_minutes": (
                    "INT",
                    {
                        "default": 15,
                        "min": 3,
                        "max": 120,
                        "tooltip": "Give up waiting after this long. Allow an extra 2-3 min when capacity "
                        "is being brought online; a warm x8 of a 1 MP photo takes about 45 s.",
                    },
                ),
                "rerun": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1_000_000,
                        "tooltip": "The same photo at the same factor is never charged twice (the earlier "
                        "result is returned). Change this number to force a fresh, paid run.",
                    },
                ),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING", "INT")
    RETURN_NAMES = ("image", "job_id", "cost_chips")
    OUTPUT_TOOLTIPS = (
        "The upscaled image(s).",
        "MegaPotato job id(s), comma-separated for a batch.",
        "Chips charged for this run (0 when an earlier result was reused).",
    )
    FUNCTION = "upscale"
    CATEGORY = "image/upscaling"
    DESCRIPTION = (
        "MegaPotato AI upscaler (megapotato.app): x1/x2/x4/x8 super-resolution over the MegaPotato "
        "API. Needs an API key from megapotato.app/account (MEGAPOTATO_API_KEY environment variable). "
        "Chips are held at submit and refunded if the job fails."
    )

    def upscale(self, image, upscale, api_key_override="", timeout_minutes=15, rerun=0, unique_id=None):
        factor = int(upscale)
        api_key = resolve_api_key(api_key_override)
        base_url = os.environ.get(ENV_API_BASE, "").strip() or DEFAULT_API_BASE

        if image.ndim == 3:
            image = image.unsqueeze(0)
        count, height, width = int(image.shape[0]), int(image.shape[1]), int(image.shape[2])
        check_dimensions(width, height, factor)

        client = MegaPotatoClient(api_key, base_url, float(timeout_minutes) * 60.0)
        progress = _Progress(100 * count, unique_id)

        # Free price check first: one readable line before any chips are held.
        quote = client.quote(width, height, factor)
        per_image = int(quote.get("cost_chips") or 0)
        progress.text(
            f"{count} image(s) {width}x{height} -> {quote.get('output_width')}x{quote.get('output_height')}, "
            f"{per_image} chip(s) each (not charged twice for a repeat of the same photo)"
        )

        state: dict = {}
        stop = threading.Event()
        pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_JOBS, count))
        try:
            futures = [pool.submit(run_one, client, image[i], factor, rerun, state, i, stop) for i in range(count)]
            while not all(f.done() for f in futures):
                time.sleep(1.0)
                if comfy_mm is not None:
                    comfy_mm.throw_exception_if_processing_interrupted()
                now = time.monotonic()
                progress.value(sum(100.0 * _fraction(state.get(i) or {}, now) for i in range(count)))
                progress.text(_status_line(state, count))
            results = [f.result() for f in futures]  # re-raises the first worker error
        finally:
            stop.set()
            pool.shutdown(wait=False, cancel_futures=True)

        progress.value(100 * count)
        tensors = [bytes_to_tensor(data) for (_, _, data) in results]
        out = torch.cat(tensors, dim=0)
        total_cost = sum(cost for (_, cost, _) in results)
        job_ids = ", ".join(job_id for (job_id, _, _) in results)
        progress.text(f"done: {out.shape[2]}x{out.shape[1]}, {total_cost} chip(s)")
        return (out, job_ids, total_cost)


NODE_CLASS_MAPPINGS = {"MegaPotatoUpscale": MegaPotatoUpscale}
NODE_DISPLAY_NAME_MAPPINGS = {"MegaPotatoUpscale": "MegaPotato Upscale (API)"}
