"""Operator-controlled legacy history import; query remains a pure-read tool."""
import json

from src.application.decision_history_import import apply_history_import, preview_history_import
from src.application.runtime_paths import resolve_runtime_root


def add_decision_history_commands(subparsers):
    parser = subparsers.add_parser("decision-history", help="decision history migration")
    commands = parser.add_subparsers(dest="history_command", required=True)
    parser = commands.add_parser("import", help="preview verified legacy JSON import; writes only with --apply and --preview-hash")
    parser.add_argument("--account", required=True)
    parser.add_argument("--market", required=True, choices=("US", "HK", "us", "hk"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preview-hash")


def handle_decision_history_command(args, *, repo_base_fn):
    from src.application.agent_tool_contracts import AgentToolError
    base = resolve_runtime_root(repo_root=repo_base_fn()).runtime_root
    try:
        if args.apply:
            if not args.preview_hash:
                raise ValueError("--apply requires --preview-hash from a reviewed preview")
            result = apply_history_import(base=base, account=args.account, market=args.market, preview_hash=args.preview_hash)
        else:
            if args.preview_hash:
                raise ValueError("--preview-hash requires --apply")
            result = preview_history_import(base=base, account=args.account, market=args.market)
    except (ValueError, OSError) as exc:
        raise AgentToolError(code="HISTORY_IMPORT_ERROR", message=str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
