# 准备提交到 GitHub

本工程已整理为独立仓库目录，尚未创建远程仓库或上传。应把 `FootballClipFactory` 目录本身作为仓库根目录，不要把它的上层交付目录、ZIP 或原本机运行目录一起上传。

## 首次本地提交

确认 `.env` 已被忽略，检查文件列表：

```powershell
git init
git branch -M main
git status --short --untracked-files=all
git add .
git diff --cached --stat
git diff --cached
```

`storage/*.py` 是必需的数据访问代码，应在提交中；`storage/codex_official/` 是个人登录状态，应被忽略。数据库、缓存、成品、原片和下载模型均不应在暂存列表中。

确认项目许可安排和暂存内容后再创建提交：

```powershell
git commit -m "Prepare Football Clip Factory source repository"
```

## 连接远程（由你决定何时执行）

在 GitHub 创建空仓库后，用它的真实地址替换示例：

```powershell
git remote add origin https://github.com/YOUR_ACCOUNT/FootballClipFactory.git
git push -u origin main
```

这些是后续手动步骤，本次整理没有执行。首次 push 后查看 Actions，确认云端依赖安装及测试结果。

## 应提交的内容

- 程序：`main.py`、`core/`、`ui/`、`storage/*.py`。
- 配置与依赖：`config/`、`prompts/`、`.env.example`、`requirements*.txt`。
- 维护：`scripts/`、`tests/`、`docs/`、根目录说明与 `.github/`。
- 空运行目录仅保留 `.gitkeep`。

不要把原安装的虚拟环境、`.env.asr`、`.env.llm`、配对配置、个人登录信息或实际项目数据添加到提交。`.gitignore` 不会移除已经进入历史的文件。
