# Shadow · STATUS

> 协作规约见全局 `~/.claude/CLAUDE.md`(Main/Lab + Try/Promote/Hotfix)。本文件 = 随时可查的当前状态。

## ✅ Main(正式 = 你的 DMG = GitHub 版本)
- **当前版本：v1.1.1**(tag,已推 GitHub)= `/Applications/Shadow.app` = `dist/Shadow.dmg`
- 文件夹：`/Users/yangxiaohui/Desktop/Claude Shadow`(分支 `main`,工作区保持干净可发布)
- 能力 = v1.0 + v1.1 + 麦克风丢轨修复。能力清单见 memory/capability_baseline_v1_1.md
- 构建配方:`BUILD.md`;依赖:`requirements.txt`
- v1.1.1 相对 v1.1:fix 蓝牙麦克风丢轨 + chore 可复现构建

## 🧪 Lab(实验 = 不碰 DMG)
- 文件夹：`/Users/yangxiaohui/Desktop/Shadow Lab`(分支 `lab`,worktree,已推 GitHub)
- 跑法：`cd "/Users/yangxiaohui/Desktop/Shadow Lab" && /usr/bin/python3 shadow_recorder.py`
- 在试什么(均未 Promote)：
  - 编辑页:裁切 / 三轨音量 / 合成预览 / 可拖可编辑字幕(点框外提交)
  - 字幕识别:whisper.cpp + VAD + 噪声门(替代苹果方案,去串扰/不截断)
  - 字幕硬烧录:Qt 渲染 PNG + ffmpeg overlay(本机 ffmpeg 无 libass)
  - 控制条移入摄像区底部;`_dur` 用 ffprobe(根治后半段字幕不显示)
  - 临时调试日志 `_dbg`(验证完删)
  - 运行时需:whisper-cli(homebrew)+ ggml-base.en.bin + ggml-silero VAD 模型(gitignore,Lab 文件夹内已有副本)
- 下一步候选方向:**跟读对照(源 vs 麦克风,词级diff+语速+停顿)** —— 仅讨论,未做。

## 待办 / 规约执行
- Promote 任何 Lab 功能前:过最小验收(录1条→确认3音轨→保存→播放),升 semver,出 DMG,推 GitHub。
- 还没做的机制:发布产物自报版本(CFBundleVersion 注入 git tag,现仍写死 1.0.0)。
- 实验持久备份:`~/ShadowLab_backup/`。
