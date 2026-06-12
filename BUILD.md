# Shadow 构建配方（可复现）

任何 tag(版本)都应能照此重建出对应 DMG。**长周期/换机维护时先读这个。**

## 构建环境（最后验证：2026-06-12）
- macOS 26.3.1
- Python: **`/usr/bin/python3`**（系统 Python 3.9.6，**不是** Homebrew）
- ffmpeg: Homebrew `8.1.1`（⚠️ 该构建**未编 libass/drawtext**，无 `ass`/`subtitles`/`drawtext` 滤镜；字幕烧录走 overlay 叠图，见 Lab）
- 签名证书：自签名 **`Shadow Self Signed`**（登录钥匙串，受信任）。来历见 memory/code_signing_cert.md

## 依赖
```
/usr/bin/python3 -m pip install -r requirements.txt
```

## 组件来源
- **Python 主程序**：`shadow_recorder.py`
- **Swift 辅助二进制**（被 .gitignore 忽略，**由 git 内的 .swift 源码重新编译**）：
  ```
  swiftc -O audio_capture.swift      -o audio_capture
  swiftc -O subtitle_recognizer.swift -o subtitle_recognizer
  swiftc -O ocr_frame.swift           -o ocr_frame
  ```
  编完由 `package_shadow.sh` 统一签名（见下）。
- **图标**：`Shadow.icns`、`ui_design/penguins_glyph.png`

## 构建 + 签名 + 打包
```
/usr/bin/python3 -m PyInstaller --noconfirm Shadow.spec      # 出 dist/Shadow.app
./package_shadow.sh install dist/Shadow.app                  # 签名 + 覆盖装到 /Applications + 去隔离
./package_shadow.sh dmg     dist/Shadow.app                  # 签名 + 生成 dist/Shadow.dmg
```

## 注意
- **录制输出目录**：spec/frozen 默认 `~/Movies/Shadow`（通用）。本机自用版会把 `OUTPUT_DIR` 改成项目内 `screentest`（构建前手动改，分发前改回）。
- **版本号**：见 `Shadow.spec` 的 `CFBundleShortVersionString`/`CFBundleVersion`（待自动注入 git tag，见 STATUS）。
- **whisper 字幕（base.en + silero VAD 模型，~148MB）属 Lab 实验**，未进 Main；Promote 时再补"模型下载脚本 + 版本记录"（模型太大不入 git）。
