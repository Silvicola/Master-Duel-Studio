namespace MDStudio.Models;

/// <summary>可导出的卡。IsChecked 可写，勾选框要双向绑定。</summary>
public sealed class PackCardItem
{
    public int Id { get; init; }
    public string Name { get; init; } = "";
    public string Kinds { get; init; } = "";
    public bool IsChecked { get; set; } = true;

    public string Label => Name.Length > 0 ? $"{Id} {Name}" : Id.ToString();
}

/// <summary>整合包里的一张卡。</summary>
public sealed class InspectItem
{
    public string Card { get; init; } = "";
    public string Name { get; init; } = "";
    public string Kind { get; init; } = "";
}
