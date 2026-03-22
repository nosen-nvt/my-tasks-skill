# CLAUDE.md

## ディスパッチャー

再起動:
```
systemctl --user restart my-tasks-dispatcher.service
```

ステータス確認:
```
systemctl --user status my-tasks-dispatcher.service --no-pager
```

## ダッシュボード

再起動:
```
systemctl --user restart my-tasks-dashboard.service
```

ステータス確認:
```
systemctl --user status my-tasks-dashboard.service --no-pager
```

注意: sandbox 内では `&&` チェインで systemctl を実行するとハングする。必ず個別に実行すること。
