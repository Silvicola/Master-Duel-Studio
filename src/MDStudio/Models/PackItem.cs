namespace MDStudio.Models;

/// <summary>导出列表里的一张卡：卡下三类内容可以各勾各的。</summary>
public sealed class PackCardItem
{
    public int Id { get; init; }
    public string Name { get; init; } = "";

    public string Label => Name.Length > 0 ? $"{Id}  {Name}" : Id.ToString();

    /// <summary>这张卡有哪些类可导。没有的类型不显示勾选框，免得让人以为漏了。</summary>
    public bool HasCutin { get; init; }
    public bool HasRunfx { get; init; }
    public bool HasArt { get; init; }
    public bool Cutin { get; set; } = true;
    public bool Runfx { get; set; } = true;
    public bool Art { get; set; } = true;

    /// <summary>一张卡至少得留一类，全不勾时退回全勾。</summary>
    public void EnsureSomething()
    {
        if (Cutin || Runfx || Art) return;
        if (HasCutin) Cutin = true;
        else if (HasRunfx) Runfx = true;
        else if (HasArt) Art = true;
    }
}

/// <summary>预览列表里的一行：包里这张卡有什么、本机现在是什么状态、能不能装。</summary>
public sealed class PackPreviewItem
{
    public int Id { get; init; }
    public string Name { get; init; } = "";

    public string Card => Id.ToString();
    public string Label => Name.Length > 0 ? $"{Id}  {Name}" : Id.ToString();

    public bool HasCutin { get; init; }
    public bool HasRunfx { get; init; }
    public bool HasArt { get; init; }

    // 状态文字：官方 / 已修改 / 无。blocked 非空时显示原因，并且不给勾。
    public string CutinTag { get; init; } = "";
    public string RunfxTag { get; init; } = "";
    public string ArtTag { get; init; } = "";

    public bool CutinEnabled { get; init; }
    public bool RunfxEnabled { get; init; }
    public bool ArtEnabled { get; init; }

    public bool Cutin { get; set; }
    public bool Runfx { get; set; }
    public bool Art { get; set; }

    /// <summary>被拦的小项汇总成一句，整行显示；没有拦截时为空。</summary>
    public string BlockNote { get; init; } = "";
    public Microsoft.UI.Xaml.Visibility BlockVisibility =>
        BlockNote.Length > 0 ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;
}

/// <summary>分享页下方「装过的包」列表里的一条。只做记录，复原在各功能页按卡进行。</summary>
public sealed class PackInstalledItem
{
    public string Id { get; init; } = "";
    public string Name { get; init; } = "";
    public string When { get; init; } = "";
    public string Summary { get; init; } = "";
}
