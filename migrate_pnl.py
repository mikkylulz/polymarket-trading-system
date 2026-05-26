"""
One-time migration: fix P&L for TP1+TP2 closed positions.

Old formula (wrong):
  TP1 P&L was discarded entirely.
  TP2 pnl = size * (1 - TP1_SIZE) * (TP2_PCT - entry)

Correct formula (shares-based):
  TP1 pnl = size * TP1_SIZE       * (TP1_PCT / entry - 1)
  TP2 pnl = size * (1 - TP1_SIZE) * (TP2_PCT / entry - 1)
  total    = tp1_pnl + tp2_pnl

Scope: only files where status='closed' and position_entry_snapshot.tp1_hit=True.
Stop-loss and resolver-resolved files use correct formulas already.
"""

import json, pathlib, sys

# ── Config constants (mirror config.json) ────────────────────
TP1_PCT  = 0.65
TP2_PCT  = 0.90
TP1_SIZE = 0.50   # fraction sold at TP1

MARKETS_DIR = pathlib.Path("data/markets")

def recalc_tp_pnl(entry: float, size: float) -> tuple:
    tp1 = size * TP1_SIZE       * (TP1_PCT / entry - 1)
    tp2 = size * (1 - TP1_SIZE) * (TP2_PCT / entry - 1)
    return round(tp1, 4), round(tp2, 4), round(tp1 + tp2, 2)

def main(dry_run: bool = False):
    files = sorted(MARKETS_DIR.glob("*.json"))
    print(f"Scanning {len(files)} market files...\n")

    candidates = []
    for p in files:
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  SKIP (parse error) {p.name}: {e}")
            continue

        if m.get("status") != "closed":
            continue

        snap = m.get("position_entry_snapshot") or m.get("position") or {}
        if not snap.get("tp1_hit"):
            continue

        entry = snap.get("price")
        size  = snap.get("size")
        if not entry or not size:
            print(f"  SKIP (missing entry/size) {p.name}")
            continue

        old_pnl = m.get("pnl")
        tp1_pnl, tp2_pnl, new_pnl = recalc_tp_pnl(entry, size)
        candidates.append({
            "path":    p,
            "mkt":     m,
            "entry":   entry,
            "size":    size,
            "old_pnl": old_pnl,
            "tp1_pnl": tp1_pnl,
            "tp2_pnl": tp2_pnl,
            "new_pnl": new_pnl,
        })

    if not candidates:
        print("No TP1+TP2 closed positions found — nothing to migrate.")
        return

    # ── Report ────────────────────────────────────────────────
    print(f"{'File':<40} {'Entry':>7} {'Size':>6} {'Old PnL':>9} {'TP1':>7} {'TP2':>7} {'New PnL':>9} {'Delta':>8}")
    print("-" * 100)

    total_old = 0.0
    total_new = 0.0

    for c in candidates:
        delta = c["new_pnl"] - (c["old_pnl"] or 0)
        total_old += c["old_pnl"] or 0
        total_new += c["new_pnl"]
        print(
            f"{c['path'].name:<40}"
            f" {c['entry']:>7.3f}"
            f" ${c['size']:>5.2f}"
            f" ${c['old_pnl']:>+8.2f}"
            f" ${c['tp1_pnl']:>+6.2f}"
            f" ${c['tp2_pnl']:>+6.2f}"
            f" ${c['new_pnl']:>+8.2f}"
            f" ${delta:>+7.2f}"
        )

    print("-" * 100)
    print(
        f"{'TOTAL':<40}"
        f" {'':>7} {'':>6}"
        f" ${total_old:>+8.2f}"
        f" {'':>7} {'':>7}"
        f" ${total_new:>+8.2f}"
        f" ${total_new - total_old:>+7.2f}"
    )

    if dry_run:
        print("\n[DRY RUN] No files written. Re-run with --apply to update.")
        return

    # ── Apply ─────────────────────────────────────────────────
    print(f"\nUpdating {len(candidates)} file(s)...")
    for c in candidates:
        c["mkt"]["pnl"] = c["new_pnl"]
        c["path"].write_text(
            json.dumps(c["mkt"], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  UPDATED {c['path'].name}  pnl: ${c['old_pnl']:+.2f} -> ${c['new_pnl']:+.2f}")

    print("\nMigration complete.")

if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    main(dry_run=dry)
