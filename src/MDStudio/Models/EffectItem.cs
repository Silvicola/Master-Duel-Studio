namespace MDStudio.Models;

/// <summary>借用发动特效时的来源卡。ready=false 表示特效资源没在盘上，借过来也放不出来。</summary>
public sealed class EffectItem
{
    public int Id { get; init; }
    public string Name { get; init; } = "";
    public string Timing { get; init; } = "";
    public string Se { get; init; } = "";
    public bool Ready { get; init; } = true;

    public string Title => string.IsNullOrEmpty(Name) ? $"卡号 {Id}" : Name;

    public string SubLine
    {
        get
        {
            var s = $"#{Id}";
            if (Timing.Length > 0) s += " · " + Timing;
            if (!Ready) s += " · 资源没下载，放不出来";
            return s;
        }
    }
}
