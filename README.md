# Learning Practice

面向秋招准备的本地学习工具：飞书管理原始题库，本地保存每日任务、作答、面试和复习记录。

## 功能

- 可调整每日代码与八股题量，按嵌套模块分配练习。
- 自由练习支持自然语言选题或随机抽题。
- 自选题目与岗位重点的面试练习。
- 每日学习热力图、个人复盘和模型错题复盘。
- 手动重新对齐飞书，记录变更并保护历史版本。
- 各环节独立提示词；Agnes 为主，OpenRouter 可手动作为备用。

## 启动

需要 Python 3.12 和 uv。在项目目录执行：

```powershell
uv sync --dev
Copy-Item .env.example .env
```

在本机 `.env` 填写对应的 `AGNES_API_KEY` 和 `OPENROUTER_API_KEY`。已有 `.env` 时不要覆盖。读取飞书需要另行安装、登录和授权 `lark-cli`。

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>。

## 数据与配置

运行数据默认保存在 `data/`，可通过 `LEARNING_DATA_DIR` 调整。仓库不包含本机密钥、数据库、媒体归档、学习记录或备份；换电脑使用时，需要单独迁移数据并配置飞书访问权限。

公开仓库不含个人飞书链接；首次运行后，在“题库与来源”登记自己的来源。已有本地数据库中的来源保持不变，可选初始配置文件 `local-config.sources.json` 也被Git忽略。测试与演示中的题源标识均为占位示例。

模型服务可能超时，部分模糊题源仍需确认；完整的候选多题拆分编辑器尚未实现。

## 验证与说明

```powershell
uv run pytest
uv run ruff check .
```

本轮已有 129 项自动化测试通过，真实外部服务的成功与失败范围见 [验收报告](ACCEPTANCE_REPORT.md)。

- [完整项目说明](README.html)（下载后用浏览器打开）
- [实现状态及剩余限制](IMPLEMENTATION_STATUS.md)
- [项目开发规则](AGENTS.md)
- [迁移与环境配置](PORTABLE_SETUP.html)
