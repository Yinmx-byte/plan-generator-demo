from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def read_state(args: argparse.Namespace) -> dict:
    if args.state_file:
        return json.loads(Path(args.state_file).read_text(encoding="utf-8-sig"))
    if args.state_json:
        return json.loads(args.state_json)
    return {}


def post_json(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(base_url: str, download_url: str, output_dir: Path, filename: str, timeout: int) -> Path:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", download_url.lstrip("/"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    with urllib.request.urlopen(url, timeout=timeout) as response:
        output_path.write_bytes(response.read())
    return output_path


def run_evaluation(
    reference_dir: str,
    candidate_docx: Path,
    state: dict,
    output: str | None,
    timeout: int,
) -> dict:
    script = Path(__file__).with_name("evaluate_plan_quality.py")
    command = [
        sys.executable,
        str(script),
        "--reference-dir",
        reference_dir,
        "--candidate-docx",
        str(candidate_docx),
        "--state-json",
        json.dumps(state, ensure_ascii=False),
    ]
    if output:
        command.extend(["--output", output])

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if process.returncode != 0:
        raise SystemExit(
            json.dumps(
                {
                    "status": "evaluation_failed",
                    "returncode": process.returncode,
                    "stdout": process.stdout,
                    "stderr": process.stderr,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if output:
        return json.loads(Path(output).read_text(encoding="utf-8"))
    return json.loads(process.stdout)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Call /api/dev/plan-test to generate a DOCX, then evaluate the generated DOCX."
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000/api/dev/plan-test")
    parser.add_argument("--state-file")
    parser.add_argument("--state-json")
    parser.add_argument("--message", default="")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--reference-dir", required=True)
    parser.add_argument("--source-skill")
    parser.add_argument("--output-dir", default=str(Path.cwd() / "iterator-output"))
    parser.add_argument("--evaluation-output")
    parser.add_argument("--request-timeout", type=int, default=240)
    parser.add_argument("--evaluation-timeout", type=int, default=120)
    args = parser.parse_args()

    state = read_state(args)
    payload = {
        "message": args.message,
        "state": state,
        "allow_partial": args.allow_partial,
        "target_skill_name": Path(args.source_skill).parent.name if args.source_skill else "",
    }
    started_at = time.time()
    plan_result = post_json(args.api_url, payload, args.request_timeout)
    if plan_result.get("status") != "generated":
        print(
            json.dumps(
                {
                    "status": "plan_generation_not_ready",
                    "plan_result": plan_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    parsed = urllib.parse.urlparse(args.api_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    candidate_docx = download_file(
        base_url,
        plan_result["download_url"],
        Path(args.output_dir),
        plan_result["filename"],
        args.request_timeout,
    )
    evaluation = run_evaluation(
        args.reference_dir,
        candidate_docx,
        state,
        args.evaluation_output,
        args.evaluation_timeout,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "elapsed_seconds": round(time.time() - started_at, 2),
                "candidate_docx": str(candidate_docx),
                "plan_result": plan_result,
                "evaluation": evaluation,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        raise SystemExit(f"Project API request failed: {exc}") from exc
