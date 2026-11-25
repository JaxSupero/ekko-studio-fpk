# Ekko Studio for fnOS

将 [EKKOLearnAI/ekko-studio](https://github.com/EKKOLearnAI/ekko-studio) 的官方 Web 发行产物打包为 **x86_64 飞牛 fnOS 原生 FPK**。本仓库仅维护打包层，非官方项目，不使用 Docker，不修改 Studio 或 Hermes Agent 的业务代码。

## Hermes Agent：跟随原版管理

**FPK 不单独固定、安装或自动追踪 Hermes Agent 的版本。**

- 首次安装 FPK 后，进入原版 Studio 的 Agent / Runtime 管理界面下载和激活 Hermes 运行时。
- Hermes 的下载源、版本列表、安装、激活、更新流程均使用原版 Studio 机制。
- 本项目没有单独追踪 NousResearch 版本的定时任务，也不会在 FPK 升级时覆盖已下载的 Agent。
- Studio 所管理的 Python、Node 等 Agent 运行时由原版自行处理，不使用旧第三方 FPK 的后台 installer。
- 首次下载运行时和后续原版更新需要联网。本 FPK **不宣称完整离线安装**。

## 安装与安全

1. 从本仓库 Releases 下载 `.fpk` 和对应 `.sha256`，核对校验和。
2. 在 fnOS 应用中心手动安装 FPK。默认监听 **8648**，端口被其他应用占用时返回错误，不终止其他应用。
3. 点击桌面图标打开原版 Studio。
4. 原版首次登录是 **admin / 123456**，请立即修改密码。未改密前不要开放公网访问；桌面仅管理员可见不等于后端端口有 fnOS 统一鉴权。
5. 按需在原版界面安装 Hermes Agent 并配置模型。未安装 Agent 时，Studio 页面本身仍应可以打开。

默认以独立的 `ekko-studio` 应用用户运行。包内保留与官方 Web 产物构建版本一致的完整 Node.js 运行时，不要求另外安装应用中心 Node.js，也不会在 NAS 上运行 npm 编译原生模块。

## 持久化与升级

| 目录 | 内容 |
| --- | --- |
| `TRIM_APPDEST/studio` | 官方 Studio 程序和生产依赖，随 FPK 更新 |
| `TRIM_APPDEST/runtime/node` | 与官方 Web 构建匹配的 Node 运行时 |
| `TRIM_PKGHOME/home` | 固定 HOME，包含默认 `.hermes` 数据 |
| `TRIM_PKGHOME/studio-data` | Studio 数据库、凭证、设置和原版管理的 `desktop-runtime` |

升级仅更换应用代码，不把整套 node_modules 复制进数据目录，不重置用户数据。卸载默认保留持久数据；只有用户明确选择删除才执行清理。请自行备份重要数据库、配置和凭证。

**Studio 程序本身建议通过本仓库 FPK 更新。** 原版界面保留上游功能，但直接使用 npm/原版自更新来替换 FPK 管理的 Studio 程序不属于本打包层保证的升级路径。这不影响 Hermes Agent 继续按原版机制更新。

## GitHub Actions

工作流：`.github/workflows/build-fpk.yml`。

- `main` 上打包代码变更：构建、测试并发布。
- 手动运行：可选择官方稳定 tag，或留空选择最高稳定语义版本；可关闭发布，仅生成 Artifact。
- 每天 UTC 00:23（北京时间 08:23）检查官方 Studio；只有相应 FPK 尚未发布完整产物时才构建。
- **仅追踪 Studio，不单独追踪 Hermes Agent。**

官方 Web UI 工作流会将部分 Release 标为非 Latest，因此本项目按正式语义版本选择，不依赖 `releases/latest`。必须同时具备官方 `.tar.gz`、版本 manifest、SHA-256 文件和 GitHub 资产 digest；下载或校验失败就终止，不回退 main 或现场 npm install。

构建使用固定 `ubuntu-22.04`，读取该官方源码提交中 Web 构建所用的精确 Node 版本。官方 fnpack 固定为 1.2.1，并校验固定 SHA-256。上游许可证和官方 Node 许可证随包保留，产物附带来源及校验信息 `build-info.json`。

FPK 版本为 `<Studio版本>-<打包修订号>`。修改打包逻辑后，需要发布同一 Studio 版本的新包时，递增根目录 `package-iteration`。

## 校验范围

每次 CI 包含：

- Python 回归测试、Shell/JavaScript 语法检查；
- 官方产物和 Node 压缩包校验，安全解包与符号链接边界检查；
- SQLite、sharp、node-pty 实际加载和 PTY 执行；
- 真实 Studio 启停、`/health`、停止幂等、端口冲突和升级数据保留检查；
- 官方 fnpack 打包，FPK 结构、权限配置、图标与 app.tgz checksum 检查。

**CI 不等于 fnOS 真机测试。** 安装、卸载、NAS 权限、原版运行时下载及实际 Agent 对话仍须在目标设备验收。当前按 Linux x86_64 构建，不宣称支持 ARM，也不对其他第三方 Hermes FPK 做自动数据迁移。

## 本地开发

```bash
python3 -m unittest discover -s tests -v
python3 scripts/resolve.py --tag v0.8.31 --output build/metadata.json
python3 scripts/build.py prepare --metadata build/metadata.json --stage build/staging
python3 scripts/smoke.py --stage build/staging
python3 scripts/build.py package --metadata build/metadata.json --stage build/staging --output dist --fnpack /path/to/fnpack
python3 scripts/verify.py dist/*.fpk
```

真实构建和冒烟测试需要 Linux x86_64、Python 3.10+、Pillow、官方 fnpack 和网络访问。GitHub API 限流时可通过 `GH_TOKEN` 提供认证；不要提交令牌或本地凭证。

## 许可证与归属

Ekko Studio 上游使用 **Business Source License 1.1**，并非本仓库可重新许可的 MIT/Apache 项目。原版许可、版权和商用限制保持不变；本仓库不授予额外商用权。FPK 将保留对应源码提交的原版 LICENSE，Node 及其依赖许可亦应一并保留。
