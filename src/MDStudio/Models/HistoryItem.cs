namespace MDStudio.Models;

/// <summary>一条改动记录。Id 是 journal 里的 id，撤销时按它来。</summary>
public sealed class HistoryItem
{
    public string Id { get; init; } = "";
    public string OpText { get; init; } = "";
    public string CardText { get; init; } = "";
    public string Time { get; init; } = "";
    public string From { get; init; } = "";

    /// <summary>能不能撤销。导出整合包这类不改游戏的操作也留记录, 但没有可撤的东西。</summary>
    public bool Undoable { get; init; } = true;
}
