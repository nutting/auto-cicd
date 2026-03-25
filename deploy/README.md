# Auto CICD

一个适合内网和离线 Maven 场景的轻量自动打包服务。

支持能力：
- 多项目配置
- Git webhook 触发构建
- 手动触发构建
- 离线 Maven 打包
- 制品归档与下载
- 钉钉通知
- 简单的构建状态页面

## 目录

- `auto_cicd_server.py`: 主服务
- `config.example.json`: 公开示例配置
- `config.json`: 你的私有实际配置，不要提交到公开仓库
- `auto-cicd.service`: systemd 示例服务文件

## 快速开始

1. 复制 `config.example.json` 为 `config.json`
2. 按你的环境修改仓库地址、认证信息、构建目录、Maven 仓库、钉钉通知
3. 启动服务：

```bash
python3 auto_cicd_server.py
```

4. 或使用 systemd：

```bash
sudo cp auto-cicd.service /etc/systemd/system/auto-cicd.service
sudo systemctl daemon-reload
sudo systemctl enable --now auto-cicd.service
```

## 接口

- 首页：`GET /`
- 构建列表：`GET /api/builds`
- 手动触发：`POST /api/build/<project_name>`
- Webhook：`POST /webhook/<project_name>`
- 制品下载：`GET /artifacts/...`
- 构建日志：`GET /logs/...`

## 配置说明

常用项目字段：
- `name`: 项目名
- `repo_url`: 仓库地址
- `repo_url_with_auth`: 带认证信息的仓库地址
- `branch`: 默认分支。Webhook 可以按实际推送分支触发构建
- `repo_dir`: 本地代码目录
- `build_subdir`: 构建根目录相对路径，可为空
- `settings_xml`: Maven settings 文件，可为空
- `maven_repo_local`: 本地 Maven 仓库
- `pre_build_modules`: 预构建模块列表
- `target_module`: 目标模块
- `package_goal`: `module` 或 `root`
- `maven_profiles`: 需要激活的 Maven profiles
- `artifact_module`: 制品所在模块
- `artifact_glob`: 制品匹配规则
- `text_replacements`: 构建前对工作副本做文本替换

## Webhook 配置

Gitea / GitLab 一般选择：
- 事件：`push`
- 内容类型：`application/json`

示例：
- `POST /webhook/project-a`

## 安全建议

- 不要把真实 `config.json` 提交到公开仓库
- 不要把钉钉 `access_token`、`secret`、仓库账号密码写进 README
- 如果你已经在 Git 历史里提交过敏感信息，公开前要额外清理历史
