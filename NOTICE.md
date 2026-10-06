# NOTICE —— 第三方组件与来源

LocPilot 自身代码以 GPL-3.0 发布（见 LICENSE）。运行时依赖、被调用工具与数据来源如下。

## 运行时引擎（外部进程 / Python 包，不随本仓库分发）

| 组件 | 许可 | 用途 | 链接 |
|------|------|------|------|
| pymobiledevice3 | GPL-3.0 | 默认定位引擎；本项目的 `engine/pmd3_worker.py` 导入其 Python API | https://github.com/doronz88/pymobiledevice3 |
| libimobiledevice (`idevicesetlocation`) | LGPL-2.1 | iOS ≤16 回退引擎（以外部命令方式调用） | https://github.com/libimobiledevice/libimobiledevice |
| go-ios | MIT | 备选引擎（以外部命令方式调用） | https://github.com/danielpaulus/go-ios |

## 前端与在线服务

| 组件 / 服务 | 许可 / 政策 | 用途 |
|------|------|------|
| Leaflet 1.9.4（本地 vendored：`web/vendor/leaflet.js`、`leaflet.css`） | BSD-2-Clause | 地图渲染 |
| OpenStreetMap 瓦片 | ODbL / 瓦片使用政策 | 底图 |
| Nominatim | 使用政策（需自定义 UA 与限流） | 地址检索与逆地理编码 |
| OSRM demo server | 使用政策（仅演示用途） | 多点路线道路路由 |

> Leaflet 以本地文件形式随仓库分发（`web/vendor/`），其许可证原文保存在 [`web/vendor/leaflet.LICENSE`](web/vendor/leaflet.LICENSE)（取自上游 v1.9.4）；`leaflet.js` 头部保留 `@preserve` 版权声明，`leaflet.css` 为无版权头的原始发行文件。

## 设计参考（仅参考思路与命令面，未复制代码）

| 项目 | 许可 | 参考内容 |
|------|------|------|
| GeoPort | GPL-3.0 | Web UI + pymobiledevice3 的组合方式、速度档位设置 |
| LocationSimulator | GPL-3.0 | macOS 端设备管理交互；其 iOS 17+ 支持已停止 |
| iFakeLocation | GPL-3.0 | 早期 iOS 设备定位工具的命令面 |
| LocWarp | MIT | Electron + Python 后端的 iAnyGo 类产品架构 |
| iAnyGo (TenoreShare) | 商业软件 | **仅作功能对标**（模式、速度、GPX、历史/收藏等），未使用其任何代码或资源 |

## 事实来源

引擎命令面与限制（`developer [dvt] simulate-location set/clear/play`、`idevicesetlocation` 的 iOS 17 硬停、
go-ios 的 `setlocation` / `tunnel start --userspace`）来自各项目官方文档/源码，以及本机安装版本的
`--help` 输出验证（pymobiledevice3 11.23.0、libimobiledevice 1.4.0）。
