namespace MDStudio.Models;

/// <summary>
/// 一条预设音效。内置的随包（不可删），用户存的在 data 目录（可删）。
/// 它只是一个现成的音频文件，选中后仍然按卡建自己的槽，不跟别的卡共用槽位。
/// </summary>
public sealed class PresetSound
{
    public string Name { get; init; } = "";
    public string Path { get; init; } = "";
    /// <summary>随程序自带的那三条，界面不给删</summary>
    public bool Builtin { get; init; }
    public double Seconds { get; init; }

    public string Text => Name;

    /// <summary>「18 秒」。读不到时长就不显示，别占一个「0 秒」的位置</summary>
    public string Duration => Seconds > 0 ? $"{Seconds:0.#} 秒" : "";
}
