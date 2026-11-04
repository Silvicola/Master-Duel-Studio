using System.ComponentModel;
using System.Runtime.CompilerServices;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace MDStudio.Models;

/// <summary>一张卡在界面上的样子。状态原值是后端给的三档：game / yours / wiped。</summary>
public sealed class CardItem : INotifyPropertyChanged
{
    public int Id { get; init; }
    public string Name { get; init; } = "";
    public string Cutin { get; init; } = "";
    public string Runfx { get; init; } = "";

    public string Title => string.IsNullOrEmpty(Name) ? $"卡号 {Id}" : Name;

    public string IdText => $"#{Id}";

    /// <summary>占位副本号时，游戏真正渲染的那个号。0 表示这张就是真卡号。</summary>
    public int Real { get; init; }

    public bool IsPlaceholder => Real > 0;

    /// <summary>列表第二行。占位卡把真卡号一并写出来，否则用户不知道该点哪一条。</summary>
    public string SubText => IsPlaceholder ? $"#{Id} · 游戏用 #{Real}" : IdText;

    /// <summary>新卡还没上线：名字在卡名表里，但不在稀有度表（开不到、合不了）。</summary>
    public bool Unreleased { get; init; }

    public Visibility UnreleasedVisibility =>
        Unreleased ? Visibility.Visible : Visibility.Collapsed;

    private bool _officialArt;
    private bool _ourArt;

    /// <summary>游戏本来就有这张卡的超框。自改之后这条标签照样留着。</summary>
    public bool OfficialArt
    {
        get => _officialArt;
        set
        {
            if (_officialArt == value) return;
            _officialArt = value;
            Notify(nameof(OfficialArtVisibility), nameof(KindOfficialArt));
        }
    }

    /// <summary>本工具给这张卡做过超框。撤销复原之后清掉。</summary>
    public bool OurArt
    {
        get => _ourArt;
        set
        {
            if (_ourArt == value) return;
            _ourArt = value;
            Notify(nameof(OurArtVisibility), nameof(KindOurArt));
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Notify(params string[] names)
    {
        foreach (var n in names)
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(n));
    }

    public Visibility PlaceholderTagVisibility =>
        IsPlaceholder ? Visibility.Visible : Visibility.Collapsed;

    public Visibility OfficialArtVisibility =>
        OfficialArt ? Visibility.Visible : Visibility.Collapsed;

    public Visibility OurArtVisibility =>
        OurArt ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>给标签取色用：official=红，self=绿（跟动画页同一套语义色）。</summary>
    public string KindOfficialArt => OfficialArt ? "official" : "none";

    public string KindOurArt => OurArt ? "self" : "none";

    /// <summary>来源卡搜索下拉里显示的一行文字（编号 + 卡名）。AutoSuggestBox 靠它显示，否则会显示类型名。</summary>
    public string Display => $"{Id} {Name}".Trim();

    /// <summary>召唤动画那格的标签文字。空=无动画，game=官方动画，自加=自改动画。</summary>
    public string TagCutin => Cutin switch
    {
        "game" => "官方动画",
        "yours" or "wiped" => "自改动画",
        _ => "无动画",
    };

    /// <summary>发动特效那格的标签文字。空=无特效，game=官方特效，自加=自改特效。</summary>
    public string TagRunfx => Runfx switch
    {
        "game" => "官方特效",
        "yours" or "wiped" => "自改特效",
        _ => "无特效",
    };

    /// <summary>取色用的类别：官方=official(红)，自加=self(绿)，无=none(灰)。</summary>
    public string KindCutin => KindOf(Cutin);

    public string KindRunfx => KindOf(Runfx);

    private static string KindOf(string state) => state switch
    {
        "game" => "official",
        "yours" or "wiped" => "self",
        _ => "none",
    };

    /// <summary>按类别取前景色。用系统刷, 浅色深色主题下都可读。</summary>
    public static Brush BrushOf(string kind)
    {
        var key = kind switch
        {
            "official" => "SystemFillColorCriticalBrush",
            "self" => "SystemFillColorSuccessBrush",
            _ => "TextFillColorSecondaryBrush",
        };
        return Application.Current.Resources.TryGetValue(key, out var v) && v is Brush b
            ? b
            : new SolidColorBrush(Colors.Gray);
    }

    /// <summary>官方自带动画：这种卡不能克隆，只能重建或导入。</summary>
    public bool Official => Cutin == "game";

    /// <summary>已经有本工具加的动画。</summary>
    public bool HasOurs => Cutin is "yours" or "wiped";

    /// <summary>已经有本工具加的特效，这时才有东西可移除。</summary>
    public bool HasOursRunfx => Runfx is "yours" or "wiped";

    /// <summary>官方自带发动特效，借用或自制都会顶掉它。</summary>
    public bool OfficialRunfx => Runfx == "game";
}
