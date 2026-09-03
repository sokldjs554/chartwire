"""CDK app entry point (spec §12.1).

Runs without the Node CLI: ``python infra/cdk/app.py`` reads ``cdk.json`` for the context flags,
applies ``cdk-nag`` ``AwsSolutionsChecks`` and writes the cloud assembly to ``infra/cdk/cdk.out``.
Only ``ChartwireStack.template.json`` is committed; CI re-synthesizes and diffs it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import cx_api
from cdk_nag import AwsSolutionsChecks

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from stack import ChartwireStack  # noqa: E402

STACK_NAME = "ChartwireStack"
REGION = "ap-northeast-2"
"""Seoul: the clinics are in Korea; a concrete region is also required for ALB access logging."""


def load_context() -> dict[str, object]:
    """Translate the CLI-level ``cdk.json`` switches into the context keys the framework reads."""
    cfg = json.loads((HERE / "cdk.json").read_text(encoding="utf-8"))
    context: dict[str, object] = dict(cfg.get("context", {}))
    context["aws:cdk:enable-path-metadata"] = bool(cfg.get("pathMetadata", False))
    context["aws:cdk:enable-asset-metadata"] = bool(cfg.get("assetMetadata", False))
    context["aws:cdk:version-reporting"] = bool(cfg.get("versionReporting", False))
    return context


def build_app(outdir: Path | None = None) -> cdk.App:
    app = cdk.App(outdir=str(outdir) if outdir else None, context=load_context(), analytics_reporting=False)
    ChartwireStack(
        app,
        STACK_NAME,
        description="chartwire — SOAPY-class 음성차팅 백엔드 층 (synth only)",
        env=cdk.Environment(region=REGION),
    )
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    return app


def main() -> int:
    app = build_app(HERE / "cdk.out")
    assembly = app.synth()
    errors = [
        m
        for m in assembly.get_stack_by_name(STACK_NAME).messages
        if m.level == cx_api.SynthesisMessageLevel.ERROR
    ]
    for m in errors:
        print(f"ERROR {m.id}: {m.entry.data}", file=sys.stderr)
    print(f"synthesized {assembly.directory} (nag errors: {len(errors)})")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
