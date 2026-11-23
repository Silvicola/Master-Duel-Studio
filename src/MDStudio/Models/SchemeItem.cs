namespace MDStudio.Models;

/// <summary>一条视频生成方案：一组画面参数，用来一键填表。</summary>
public sealed class SchemeItem
{
    public string Name { get; set; } = "";
    /// <summary>内置方案（2K / 4K）：不给删、不给覆盖，也不写进 schemes.json</summary>
    public bool Builtin { get; set; }
    public int Frames { get; set; } = 72;
    public int Cell { get; set; } = 1664;
    public double Dur { get; set; } = 1.6;
    public int Size { get; set; } = 2880;
    public int OffX { get; set; }
    public int OffY { get; set; }
    public int Rot { get; set; }
    public double Zoom { get; set; } = 1.0;
    /// <summary>超分倍数：0 = 关闭，2 / 4 = 先放大再缩回</summary>
    public int Upscale { get; set; }
    /// <summary>曲线预设下标：0 = Silvicola 配置，1 = 自定义</summary>
    public int Preset { get; set; }
    public string Curve { get; set; } = "";
    public bool Pop { get; set; } = true;
}
