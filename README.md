# Master Duel Studio

《游戏王 Master Duel》的本地修改工具：召唤动画、发动特效、卡图（超框 / Rush Duel 风格）、大厅壁纸，全部在一个 WinUI 3 程序里完成，所有改动可撤销。

> 仅支持 PC 版（Steam）。软件不修改游戏本体，只写入你自己的账号数据目录。

## 功能

- **召唤动画**：借用官方来源卡、用视频自制（AI 抠像 / 方形模式），或从 Spine 骨骼文件导入
- **发动特效**：借用官方效果，或用视频 / GIF / 帧文件夹自制，可替换音效槽
- **卡图**：超框（主体跨出框线，棱彩 / 虹彩 / 幻彩 / 官方四种边框效果）与 Rush Duel 风格整卡重绘
- **壁纸**：替换大厅壁纸与大厅背景（含 `data.unity3d` 内的背景纹理）
- **整合包**：按卡号把一张卡的动画、特效、卡图一起打包，发给别人即可安装
- **历史**：每一次改动都可单独撤销；游戏更新后可一键重新同步
- **预演模式**：只显示将要写入的文件，不落盘

## 界面

<table>
  <tr>
    <td align="center"><img src="docs/demo/page-anim.png" width="280" alt="动画页"><br>动画页</td>
    <td align="center"><img src="docs/demo/page-cardart.png" width="280" alt="卡图页"><br>卡图页</td>
    <td align="center"><img src="docs/demo/page-wallpaper.png" width="280" alt="壁纸页"><br>壁纸页</td>
  </tr>
</table>

## 效果示例

连接怪兽超框，主体跨出框线，点亮方向的连接箭头压在主体之上：

<a href="docs/demo/overframe-demo.png"><img src="docs/demo/overframe-demo.png" width="320" alt="超框效果"></a>

## 演示

**召唤动画**

<img src="docs/demo/cutin-demo.gif" width="480" alt="召唤动画演示">

**发动特效**

<img src="docs/demo/runfx-demo.gif" width="480" alt="发动特效演示">

## 视频教程

完整流程演示（B 站）：**<https://www.bilibili.com/video/BV1BWx962EWS/>**

## 使用说明

1. 下载并解压便携版，双击「启动.cmd」
2. 首次启动会自动寻找游戏目录（Steam 注册表、Steam 库目录、常见安装位置依次尝试），找不到时才需要手动选
3. 写入前请完全退出游戏，写入完成后重新启动游戏生效
4. 支持 Windows 10 1809 及以上

## 构建

需要 [Windows App SDK](https://learn.microsoft.com/windows/apps/windows-app-sdk/) 运行环境：

```bat
dotnet build src/MDStudio -c Release
```

打包便携版（解压即用，自动收集 Python 运行时与素材）：

```bat
python tools/build_portable.py
```

抠像模型 `tools/matting/isnet-anime.onnx` 体积过大不入库，首次使用会自动下载。

## 致谢

感谢以下开源项目：

- [MD-Card-Mod-Tool](https://github.com/noah-ad/MD-Card-Mod-Tool)
- [floowandereeze-and-modding-qt](https://github.com/Nauder/floowandereeze-and-modding-qt)
- [master-duel-modding](https://github.com/AmidoriA/master-duel-modding)

以及感谢 DeepSeek 大肥鱼的背后默默付出！

## 推荐搭配

本软件搭配 [spineviewer-love](https://github.com/yihkllo/spineviewer-love) 与 [冷晴MD工具箱](https://www.bilibili.com/video/BV1yhpw6uE5B) 使用更佳！

## 许可证

[MIT](LICENSE)
