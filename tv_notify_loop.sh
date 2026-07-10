#!/bin/zsh
# 旧TradingView通知ループは廃止。Discordはfx_briefing_loop.shが送る
# 5分ごとの「FXシグナルボード」1通だけに統一する。
print -u2 "tv_notify_loop.sh は廃止されました。./fx_briefing_loop.sh を起動してください。"
exit 0
