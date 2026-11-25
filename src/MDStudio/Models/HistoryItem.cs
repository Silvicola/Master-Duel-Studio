using System;
using System.Collections.Generic;
using System.ComponentModel;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace MDStudio.Models;

/// <summary>一条历史记录：三类改动之一，或一条复原留痕。一次操作算一条。</summary>
public sealed class HistoryItem : INotifyPropertyChanged
{
    /// <summary>时间，格式 YYYY-MM-DD HH:mm。</summary>
    public string Time { get; init; } = "";

    /// <summary>行内只显示时分，日期交给分组头。</summary>
    public string Clock { get; init; } = "";

    /// <summary>用来切「最近三天」的窗口和按天分组。</summary>
    public DateTime When { get; init; }

    /// <summary>分组头（不是记录）：整行只显示一句日期标题。</summary>
    public bool IsHeader { get; init; }
    public string HeaderText { get; init; } = "";
    public Visibility HeaderVisibility => IsHeader ? Visibility.Visible : Visibility.Collapsed;
    public Visibility RowVisibility => IsHeader ? Visibility.Collapsed : Visibility.Visible;

    /// <summary>历史列表里插入的日期分隔行。</summary>
    internal static HistoryItem MakeHeader(string text)
        => new() { IsHeader = true, HeaderText = text };

    /// <summary>类别徽章文字：动画 / 特效 / 超框卡图 / 复原。</summary>
    public string Category { get; init; } = "";

    /// <summary>筛选键：all / cutin / runfx / art。复原条目跟被撤销的类别走。</summary>
    public string Filter { get; init; } = "all";

    /// <summary>召唤动画的导入方式徽章：视频 / Spine。其他类别为空。</summary>
    public string Method { get; init; } = "";
    public Visibility MethodVisibility => Method.Length > 0 ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>复原留痕。标题用次级色、不显示次数胶囊。</summary>
    public bool IsUndo { get; init; }

    /// <summary>操作的中文名。</summary>
    public string OpText { get; init; } = "";

    /// <summary>对象：卡号加卡名。</summary>
    public string CardText { get; init; } = "";

    /// <summary>右侧第一行：来源、覆盖数。</summary>
    public string Detail1 { get; init; } = "";

    /// <summary>右侧第二行：改动文件数等。</summary>
    public string Detail2 { get; init; } = "";

    /// <summary>导出这类不改游戏文件的记录。</summary>
    public bool ReadOnly { get; init; }

    /// <summary>悬停时补充的细节。没有额外信息时为 null。</summary>
    public string? Tip { get; init; }

    public Visibility ReadOnlyVisibility => ReadOnly ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>复原条目的标题用次级色，正常条目用主色。</summary>
    public Brush TitleForeground => Pick(IsUndo ? "HistTextOther" : "TextFillColorPrimaryBrush");

    /// <summary>左侧 3px 浅色条。四档类别各一色，浅底深字配浅色条。</summary>
    public Brush CategoryBrush => BarOf(Category);

    /// <summary>类别徽标的浅色底。</summary>
    public Brush CategoryBadgeBrush => BadgeOf(Category);

    /// <summary>类别徽标的同色系深字。</summary>
    public Brush CategoryTextBrush => TextOf(Category);

    // 四档类别配色放在 App.xaml 的 Light / Dark 两套 ThemeDictionaries 里
    //（2026-11-17 用户定稿组 1：动画紫 / 特效青 / 超框玫粉 / 复原与其他灰）。
    private static string KeyOf(string category) => category switch
    {
        "动画" => "Anim",
        // 历史页的类别徽章文字是「卡牌特效」，不是「特效」；以前这里只认后者，
        // 落到了默认的灰色，跟「复原」撞色（2026-11-23 用户实测）。
        "卡牌特效" or "特效" => "Fx",
        "超框卡图" => "Art",
        _ => "Other",
    };

    internal static Brush BadgeOf(string category) => Pick($"HistBadge{KeyOf(category)}");
    internal static Brush TextOf(string category) => Pick($"HistText{KeyOf(category)}");
    internal static Brush BarOf(string category) => Pick($"HistBar{KeyOf(category)}");

    private static Brush Pick(string key)
    {
        var res = Application.Current.Resources;
        bool dark = (App.MainWindow?.Content as FrameworkElement)?.ActualTheme == ElementTheme.Dark;
        if (res.ThemeDictionaries.TryGetValue(dark ? "Dark" : "Light", out var dict)
            && dict is ResourceDictionary rd
            && rd.TryGetValue(key, out var v) && v is Brush b)
            return b;
        return new SolidColorBrush(Microsoft.UI.Colors.Gray);
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Notify(params string[] names)
    {
        foreach (var n in names) PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(n));
    }
}
