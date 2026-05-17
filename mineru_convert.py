#!/usr/bin/env python3
"""
MinerU PDF to Markdown Converter

Converts PDF files to Markdown using the MinerU Precise Parsing API (v4).
Automatically splits PDFs exceeding 200MB or 200 pages into chunks,
converts each chunk, and merges the resulting markdown.

Requirements:
    pip install requests PyPDF2

Usage:
    export MINERU_TOKEN="your_token"
    python mineru_convert.py input.pdf
    python mineru_convert.py input.pdf -o output.md --language en
"""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional

import requests
from PyPDF2 import PdfReader, PdfWriter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://mineru.net"
MAX_FILE_SIZE_MB = 200
MAX_PAGES = 200
MAX_BATCH_FILES = 50
POLL_INTERVAL = 5
DEFAULT_TIMEOUT = 3600


# ---------------------------------------------------------------------------
# MinerU API Client
# ---------------------------------------------------------------------------

class MinerUClient:
    """Client for the MinerU Precise Parsing API (v4)."""

    def __init__(self, token: str, base_url: str = BASE_URL):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def upload_files(
        self,
        file_paths: List[str],
        model_version: str = "pipeline",
        enable_formula: bool = True,
        enable_table: bool = True,
        language: str = "ch",
        is_ocr: bool = False,
        extra_formats: Optional[List[str]] = None,
    ) -> str:
        """
        Upload local files for batch processing via signed URLs.

        Returns the batch_id for polling.
        """
        files_payload = [{"name": os.path.basename(p)} for p in file_paths]

        body = {
            "files": files_payload,
            "model_version": model_version,
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "language": language,
            "is_ocr": is_ocr,
        }
        if extra_formats:
            body["extra_formats"] = extra_formats

        # Step 1: Get signed upload URLs
        resp = requests.post(
            f"{self.base_url}/api/v4/file-urls/batch",
            json=body,
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get upload URLs: {data}")

        batch_id = data["data"]["batch_id"]
        file_urls = data["data"]["file_urls"]

        if len(file_urls) != len(file_paths):
            raise RuntimeError("Mismatch between files and returned URLs")

        # Step 2: PUT each file to its signed URL
        for path, url in zip(file_paths, file_urls):
            with open(path, "rb") as f:
                file_data = f.read()
            put_resp = requests.put(url, data=file_data)
            if put_resp.status_code not in (200, 201, 204):
                raise RuntimeError(
                    f"Upload failed for {os.path.basename(path)}: "
                    f"HTTP {put_resp.status_code}"
                )

        return batch_id

    def poll_batch(
        self,
        batch_id: str,
        poll_interval: int = POLL_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> List[dict]:
        """Poll for batch results. Returns list of result dicts."""
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                raise TimeoutError(f"Batch {batch_id} timed out after {timeout}s")

            resp = requests.get(
                f"{self.base_url}/api/v4/extract-results/batch/{batch_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                raise RuntimeError(f"Error polling batch: {data}")

            results = data["data"]["extract_result"]
            states = {r["state"] for r in results}

            if "failed" in states:
                failed = [r for r in results if r["state"] == "failed"]
                msgs = [f"{f['file_name']}: {f.get('err_msg', 'unknown')}" for f in failed]
                raise RuntimeError(f"Tasks failed: {'; '.join(msgs)}")

            if states == {"done"}:
                return results

            done = sum(1 for r in results if r["state"] == "done")
            print(f"  [{done}/{len(results)}] done, {elapsed:.0f}s elapsed", end="\r")
            time.sleep(poll_interval)

    def download_and_extract(self, result: dict, output_dir: str) -> str:
        """Download the result ZIP and extract full.md. Returns path to .md file."""
        zip_url = result["full_zip_url"]
        file_name = result["file_name"]
        base_name = Path(file_name).stem

        resp = requests.get(zip_url)
        resp.raise_for_status()

        os.makedirs(output_dir, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            md_names = [n for n in zf.namelist() if n.endswith(".md")]
            src = "full.md" if "full.md" in md_names else md_names[0]
            content = zf.read(src).decode("utf-8")

        md_path = os.path.join(output_dir, f"{base_name}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(content)

        return md_path


# ---------------------------------------------------------------------------
# PDF Splitter
# ---------------------------------------------------------------------------

class PDFSplitter:
    """Split large PDFs into chunks that satisfy MinerU limits."""

    def __init__(self, max_size_mb: int = MAX_FILE_SIZE_MB, max_pages: int = MAX_PAGES):
        self.max_size_mb = max_size_mb
        self.max_pages = max_pages
        self.max_size_bytes = max_size_mb * 1024 * 1024

    def needs_split(self, pdf_path: str) -> bool:
        size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        reader = PdfReader(pdf_path)
        return size_mb > self.max_size_mb or len(reader.pages) > self.max_pages

    def get_page_count(self, pdf_path: str) -> int:
        return len(PdfReader(pdf_path).pages)

    def split(self, pdf_path: str, output_dir: str) -> List[str]:
        """
        Split a PDF into chunks that satisfy both size and page limits.

        Uses a size-estimation approach: if the average bytes-per-page
        suggests fewer than max_pages are needed to stay under the size
        limit, we use that smaller page count per chunk. If a resulting
        chunk still exceeds the size limit (unusual with variable page
        sizes), it gets re-split recursively.
        """
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        total_size = os.path.getsize(pdf_path)
        avg_bytes_per_page = total_size / max(total_pages, 1)

        # Estimate pages per chunk to stay under the size limit
        pages_for_size = int(self.max_size_bytes / avg_bytes_per_page) if avg_bytes_per_page else self.max_pages
        chunk_pages = min(self.max_pages, pages_for_size)
        chunk_pages = max(1, chunk_pages)

        chunks = []
        base_name = Path(pdf_path).stem

        for start in range(0, total_pages, chunk_pages):
            end = min(start + chunk_pages, total_pages)
            chunk_name = f"{base_name}_part_{len(chunks) + 1:03d}.pdf"
            chunk_path = os.path.join(output_dir, chunk_name)

            writer = PdfWriter()
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            with open(chunk_path, "wb") as f:
                writer.write(f)

            chunk_size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
            if chunk_size_mb > self.max_size_mb:
                # Re-split with half the pages
                os.remove(chunk_path)
                sub_splitter = PDFSplitter(self.max_size_mb, max(chunk_pages // 2, 1))
                temp_path = os.path.join(output_dir, f"_temp_{chunk_name}")
                temp_writer = PdfWriter()
                for i in range(start, end):
                    temp_writer.add_page(reader.pages[i])
                with open(temp_path, "wb") as f:
                    temp_writer.write(f)
                sub_chunks = sub_splitter.split(temp_path, output_dir)
                os.remove(temp_path)
                chunks.extend(sub_chunks)
            else:
                chunks.append(chunk_path)
                print(f"  {chunk_name}: {end - start} pages, {chunk_size_mb:.1f}MB")

        return chunks


# ---------------------------------------------------------------------------
# Markdown Merger
# ---------------------------------------------------------------------------

def merge_markdown(md_paths: List[str], output_path: str) -> None:
    """Concatenate markdown files in sorted order."""
    md_paths = sorted(md_paths)
    with open(output_path, "w", encoding="utf-8") as out:
        for i, path in enumerate(md_paths):
            with open(path, "r", encoding="utf-8") as f:
                out.write(f.read())
            if i < len(md_paths) - 1:
                out.write("\n\n---\n\n")


# ---------------------------------------------------------------------------
# Single PDF processing
# ---------------------------------------------------------------------------

def process_single_pdf(
    client: MinerUClient,
    splitter: PDFSplitter,
    pdf_path: str,
    output_path: str,
    args: argparse.Namespace,
) -> None:
    """Convert a single PDF to markdown, splitting if needed."""
    work_dir = tempfile.mkdtemp(prefix="mineru_")
    try:
        # Step 1: Split if needed
        if splitter.needs_split(pdf_path):
            total_pages = splitter.get_page_count(pdf_path)
            total_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            print(f"  {total_mb:.1f}MB, {total_pages} pages — splitting...")
            chunk_pdfs = splitter.split(pdf_path, work_dir)
            print(f"  Split into {len(chunk_pdfs)} chunk(s).")
        else:
            chunk_pdfs = [pdf_path]

        # Step 2: Upload in batches (API allows max 50 files per batch)
        all_results = []
        for batch_idx in range(0, len(chunk_pdfs), MAX_BATCH_FILES):
            batch = chunk_pdfs[batch_idx:batch_idx + MAX_BATCH_FILES]
            batch_num = batch_idx // MAX_BATCH_FILES + 1
            print(f"  Uploading batch {batch_num} ({len(batch)} file(s))...")

            batch_id = client.upload_files(
                batch,
                model_version=args.model,
                enable_formula=not args.no_formula,
                enable_table=not args.no_table,
                language=args.language,
                is_ocr=args.ocr,
                extra_formats=args.extra_formats,
            )

            print("  Waiting for parsing...")
            results = client.poll_batch(
                batch_id,
                poll_interval=args.poll_interval,
                timeout=args.timeout,
            )
            all_results.extend(results)

        # Step 3: Download and extract
        md_dir = os.path.join(work_dir, "md")
        md_files = []
        for result in all_results:
            md_path = client.download_and_extract(result, md_dir)
            md_files.append(md_path)

        # Step 4: Merge or copy
        if len(md_files) > 1:
            merge_markdown(md_files, output_path)
        else:
            shutil.copy(md_files[0], output_path)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert PDF to Markdown using MinerU Precise Parsing API"
    )
    parser.add_argument(
        "input", help="Path to input PDF file or directory containing PDFs"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output path: file (if input is a file) or directory (if input is a directory)",
    )
    parser.add_argument(
        "--token", default=None,
        help="MinerU API token (default: read from config.json)",
    )
    parser.add_argument("--language", default="ch", help="Document language (default: ch)")
    parser.add_argument(
        "--model", default="pipeline", choices=["pipeline", "vlm"],
        help="Model version (default: pipeline)",
    )
    parser.add_argument("--ocr", action="store_true", help="Enable OCR")
    parser.add_argument("--no-formula", action="store_true", help="Disable formula recognition")
    parser.add_argument("--no-table", action="store_true", help="Disable table recognition")
    parser.add_argument(
        "--extra-formats", nargs="*", choices=["docx", "html", "latex"],
        help="Additional output formats",
    )
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
                        help=f"Seconds between status checks (default: {POLL_INTERVAL})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Max wait time in seconds (default: {DEFAULT_TIMEOUT})")

    args = parser.parse_args()

    # Token resolution: CLI arg > env var > config.json
    token = args.token or os.environ.get("MINERU_TOKEN")
    if not token:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "config.json")
        if os.path.isfile(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            token = config.get("token", "").strip()
            if token == "your_token_here":
                token = ""
    if not token:
        print("Error: API token required. Put it in config.json, set MINERU_TOKEN env var, or use --token.")
        sys.exit(1)

    input_path = args.input

    # Determine mode: single file or directory
    if os.path.isfile(input_path):
        # --- Single file mode ---
        output_path = args.output or f"{Path(input_path).stem}.md"
        client = MinerUClient(token=token)
        splitter = PDFSplitter()
        process_single_pdf(client, splitter, input_path, output_path, args)
        print(f"Done — {output_path}")

    elif os.path.isdir(input_path):
        # --- Directory mode ---
        output_dir = args.output or input_path
        os.makedirs(output_dir, exist_ok=True)

        # Collect all PDFs
        pdf_files = sorted(
            p for p in Path(input_path).iterdir()
            if p.suffix.lower() == ".pdf" and p.is_file()
        )
        if not pdf_files:
            print(f"No PDF files found in {input_path}")
            sys.exit(0)

        # Filter: skip already-converted files
        pending = []
        skipped = []
        for pdf_path in pdf_files:
            md_name = pdf_path.stem + ".md"
            md_path = os.path.join(output_dir, md_name)
            if os.path.exists(md_path):
                skipped.append(pdf_path.name)
            else:
                pending.append((str(pdf_path), md_path))

        print(f"Found {len(pdf_files)} PDF(s)")
        if skipped:
            print(f"  Skipping {len(skipped)} already converted: {', '.join(skipped)}")
        if not pending:
            print("Nothing to convert.")
            sys.exit(0)

        print(f"  Converting {len(pending)} file(s)\n")

        client = MinerUClient(token=token)
        splitter = PDFSplitter()

        for i, (pdf_path, md_path) in enumerate(pending):
            name = os.path.basename(pdf_path)
            print(f"[{i + 1}/{len(pending)}] {name}")
            try:
                process_single_pdf(client, splitter, pdf_path, md_path, args)
                print(f"  -> {md_path}\n")
            except Exception as e:
                print(f"  Failed: {e}\n", file=sys.stderr)
                continue

        print("All done.")

    else:
        print(f"Error: {input_path} is not a valid file or directory.")
        sys.exit(1)


if __name__ == "__main__":
    main()
