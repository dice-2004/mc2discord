# mc2discord

Minecraft サーバーを Docker 環境で管理するための Discord Bot

概要:
- Docker 上で稼働する Minecraft サーバー（RCON 有効）を Discord から操作・監視します。
- コンテナのログを監視して入退室・起動完了・予期せぬ停止を通知します。

主な機能:
- `/mc start` `/mc stop` `/mc restart` `/mc status` のスラッシュコマンド
- RCON を用いた `save-all` や `say` 実行
- コンテナのログストリーム監視による入退室通知
- Bot ステータスに稼働状態とプレイヤー数を表示

セットアップ:
1. リポジトリをクローン
2. 環境変数を `.env` で設定（テンプレートは `.env.example` を参照）
3. Docker イメージをビルドして起動

開発・ビルド例:
```bash
# ビルド
docker build -t mc2discord:latest .

# 実行（ホストの docker.sock をマウントする必要あり）
docker run -e DISCORD_TOKEN="$DISCORD_TOKEN" \
  -e CONTAINER_NAME="$CONTAINER_NAME" \
  -e RCON_HOST="$RCON_HOST" -e RCON_PORT="$RCON_PORT" -e RCON_PASSWORD="$RCON_PASSWORD" \
  -e CHANNEL_ID="$CHANNEL_ID" -e GUILD_ID="$GUILD_ID" -e ADMIN_ROLE_ID="$ADMIN_ROLE_ID" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  mc2discord:latest
```

詳しい設定は `src/config.py` を参照してください。
