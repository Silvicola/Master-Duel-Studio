namespace MDStudio.Models;

/// <summary>
/// 一个发动特效用的音效槽。
/// OnDisk=false 分两种情况：内置于游戏主程序的文件（游戏里照常响，只是没法换），
/// 和根本没下载的（换了也是静音）。两者文案要分开，否则会让人以为这张卡的音效是坏的。
/// </summary>
public sealed class SoundSlot
{
    public string Label { get; init; } = "";
    public bool OnDisk { get; init; }
    public bool Builtin { get; init; }
    public string Name { get; init; } = "";
    public List<int> Cards { get; init; } = new();

    /// <summary>界面上用「卡名 #编号」指代。SE_EV_ 那串内部名只在查不到卡时才露出来。</summary>
    public string CardText => Cards.Count > 0 ? $"{Name} #{Cards[0]}".Trim() : Label;

    /// <summary>下拉框里的一行：谁在用 + 能不能换。</summary>
    public string Text => OnDisk
        ? $"{CardText}（{Cards.Count} 张卡在用）"
        : Builtin
            ? $"{CardText}（内置音效，游戏里正常播放，本工具无法替换）"
            : $"{CardText}（音频文件没下载，换了也是静音）";
}
