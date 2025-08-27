# iPhone WDA Mirror

通过 [WebDriverAgent](https://github.com/appium/WebDriverAgent) 获取 iPhone 屏幕并使用鼠标控制设备。本项目整理为标准的 Python `src` 目录结构，方便二次开发与分发。

## 安装

1. 确保设备已经部署并启动 WDA 服务，USB 连接时可使用 `iproxy 8100 8100` 转发端口。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

## 使用

运行模块即可启动镜像窗口：

```bash
python -m iphone_wda_mirror
```

窗口中左键点击相当于点按，按住拖动可以实现滑动，按 `Esc` 或 `q` 退出。

如需固定附着某个 App，可编辑 `src/iphone_wda_mirror/mirror.py` 中的 `TARGET_BUNDLE`。默认附着当前前台应用。

## 目录结构

```
.
├── README.md
├── requirements.txt
├── src
│   └── iphone_wda_mirror
│       ├── __init__.py
│       ├── __main__.py
│       └── mirror.py
└── tests
```

## 许可证

MIT
