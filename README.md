# 球赛智析

本项目是足球比赛预测本地 Web App 的 MVP 原型。

## 启动

```bash
python3 app.py
```

然后打开 http://127.0.0.1:8765 。首次启动会创建 `football_predict.db`，并写入少量演示赛事和球队统计数据。

## 当前能力

- 本地 SQLite 赛事存储
- 赛事列表和联赛筛选
- 基于 Poisson 的胜平负、总进球、半全场和比分概率
- 保存每次预测结果
- 预测历史查询
- 赛后比分录入 API
- 赛事批量导入 API
- 本地图片 OCR 适配器与赛事候选匹配（依赖可用的 macOS Swift/Vision 工具链）
- 预留公开数据采集适配器

## 同步公开赛事

项目支持使用 [football-data.org v4 比赛接口](https://docs.football-data.org/general/v4/competition.html) 同步英超、西甲、德甲、意甲和法甲未来 14 天赛事。先从该服务获取 API token，然后在启动前设置：

```bash
export FOOTBALL_DATA_TOKEN="你的 token"
python3 app.py
```

在页面点击“同步公开赛事”即可。token 只在本地后端进程环境中使用，不会发送到浏览器或写入数据库。

## 本地 OCR

截图识别使用 macOS 的 Vision 框架。若上传图片提示 Swift/Vision 工具链不可用，请先更新或重新安装 Xcode Command Line Tools，然后重启应用；图片会保留在本地 `uploads/` 目录中。

当前数据是演示数据，不应视为实时数据或实际预测建议。
