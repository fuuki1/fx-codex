"""判断を再計算するための最小限の来歴(code_hash / config_hash)。

判断ログには既に `decision_id` / `input_context_id` / `source_record_ids` /
`label_version` があり、「どの入力から出た判断か」は辿れる。しかし
**「どのコードとどの設定で出た判断か」は記録されていない**ため、
過去の判断を再計算しても同じ結果になる保証がなかった。

ここで足すのは2つだけ。

- `code_hash`   … 判断を決めるモジュール群の内容ハッシュ
- `config_hash` … 判断を左右する定数・ポリシーのハッシュ

どちらも「値が変わったら判断も変わりうる」ものだけを対象にする。
広く取りすぎると無関係な変更で毎回変わり、狭すぎると再現性の証明にならない。

⚠️ 実行時に計算するとbriefingごとにファイルを読み直すことになるため、
プロセス内で一度だけ計算してキャッシュする(内容は実行中に変わらない)。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

PROVENANCE_CONTRACT = "decision-provenance-v1"

# 判断結果を直接決めるモジュール。ここが変われば同じ入力でも判断が変わりうる。
# ⚠️ 採点・学習側(learning/trade_outcome)は「判断そのもの」ではなく事後評価なので
# 含めない。含めると採点ロジックを触るたびに過去judgmentのcode_hashが変わり、
# 「判断が変わっていない」ことを示せなくなる。
DECISION_CODE_MODULES: tuple[str, ...] = (
    "briefing.py",
    "timeframe.py",
    "committee.py",
    "technicals.py",
    "direction_threshold.py",
    "journal.py",
)

_MODULE_ROOT = Path(__file__).resolve().parent


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=1)
def code_hash() -> str:
    """判断を決めるモジュール群の内容ハッシュ。

    ファイル名とバイト列を順序固定で連結して1つのsha256にする。
    読めないモジュールがあっても例外にせず、その事実をハッシュに含める
    (判断は続行できるべきで、来歴が欠けたことは値の違いとして残る)。
    """
    digest = hashlib.sha256()
    for name in sorted(DECISION_CODE_MODULES):
        digest.update(name.encode("utf-8"))
        try:
            digest.update(_sha256_bytes((_MODULE_ROOT / name).read_bytes()).encode())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()


def config_hash(values: dict[str, object] | None = None) -> str:
    """判断を左右する設定値のハッシュ。

    values を渡さない場合は既定の判断パラメータから組む。呼び出し側が
    実際に使った値を渡せば、そちらを正としてハッシュする。
    """
    payload = values if values is not None else default_decision_config()
    return _sha256_bytes(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    )


def default_decision_config() -> dict[str, object]:
    """判断結果を左右する既定パラメータを集める。

    ⚠️ 循環importを避けるため関数内でimportする(briefing/journal 側からも
    このモジュールを読むため)。
    """
    from . import journal
    from .briefing import CONFLICT_THRESHOLD, NEWS_WEIGHT, TECH_WEIGHT

    return {
        "conflict_threshold": CONFLICT_THRESHOLD,
        "news_weight": NEWS_WEIGHT,
        "tech_weight": TECH_WEIGHT,
        "horizon_hours": journal.DEFAULT_HORIZON_HOURS,
        "tolerance_hours": journal.DEFAULT_TOLERANCE_HOURS,
        "atr_fraction": journal.DEFAULT_ATR_FRACTION,
    }


def decision_provenance(config: dict[str, object] | None = None) -> dict[str, str]:
    """判断行に載せる最小限の来歴。"""
    return {
        "provenance_contract": PROVENANCE_CONTRACT,
        "code_hash": code_hash(),
        "config_hash": config_hash(config),
    }
