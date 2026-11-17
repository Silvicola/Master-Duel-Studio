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

    /// <summary>
    /// 动画资源实际归属：0 或等于自己 = 有自己的动画；别的卡号 = 借用那张卡的动画。
    /// 官方超框变体（#22804）没有自己的资源，播的是底图（#14952）那套。
    /// </summary>
    public int CutinSrc { get; init; }

    public string CutinSrcName { get; init; } = "";

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

    /// <summary>游戏出现了语义表里没有的 subtype，卡框没确认，不能合成也不能写入。</summary>
    public bool UnknownSubtype { get; init; }

    public Visibility UnknownSubtypeVisibility =>
        UnknownSubtype ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>动态索引判定这张卡当前不可操作（未上线 / 卡型未确认 / 索引解析失败）。</summary>
    public bool Locked { get; init; }

    public Visibility LockVisibility => Locked ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>卡型中文名与粗类，来自随包语义表，供详情区显示。</summary>
    public string SubtypeCn { get; init; } = "";
    public string CoarseCn { get; init; } = "";
    public string AbilitiesText { get; init; } = "";

    /// <summary>锁定的具体原因。卡型未确认要写清楚是维护侧需要补语义表。</summary>
    public string? LockTip => UnknownSubtype
        ? "游戏出现未识别的卡型，卡框未确认，已锁定；详见 data/carddata_warnings.log"
        : Unreleased ? "这张卡还没上线，暂时不能修改"
        : Locked ? "卡数据无法解析，已禁用写入" : null;

    private bool _officialArt;
    private bool _ourArt;

    /// <summary>游戏本来就有这张卡的超框。</summary>
    public bool OfficialArt
    {
        get => _officialArt;
        set
        {
            if (_officialArt == value) return;
            _officialArt = value;
            Notify(nameof(OfficialArtVisibility), nameof(KindOfficialArt),
                   nameof(ArtKind), nameof(ArtTag), nameof(ArtTagVisibility), nameof(ArtTagTip), nameof(KindArt));
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
            Notify(nameof(OurArtVisibility), nameof(KindOurArt),
                   nameof(ArtKind), nameof(ArtTag), nameof(ArtTagVisibility), nameof(ArtTagTip), nameof(KindArt));
        }
    }

    /// <summary>
    /// 超框状态的四个互斥分档，和左侧筛选下拉、后端 search 的 art 参数同一套词。
    ///
    /// 2026-11-17 用户定稿：标签只回答「现在这套是谁的」，官方被覆盖这件事交给
    /// 悬停讲（和动画页「自改动画」的做法一致）。所以 selfOfficial 与 self 都显示
    /// 「自改超框」，筛选也合并成一档。
    /// </summary>
    public string ArtKind =>
        _officialArt && _ourArt ? "selfOfficial"
        : _officialArt ? "official"
        : _ourArt ? "self"
        : "none";

    public string ArtTag => ArtKind switch
    {
        "official" => "官方超框",
        "selfOfficial" or "self" => "自改超框",
        _ => "",
    };

    public Visibility ArtTagVisibility =>
        ArtTag.Length > 0 ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>自改那一行只讲来历，操作提示由右侧说明与「复原」按钮承担。</summary>
    public string ArtTagTip => ArtKind switch
    {
        "official" => "游戏自带这张卡的超框，写入会覆盖它。",
        "selfOfficial" => "来自官方超框",
        "self" => "来自无超框",
        _ => "这张卡没有超框。",
    };

    /// <summary>标签取色：官方=红，自改（含★）=绿，和动画页同一套语义色。</summary>
    public string KindArt => ArtKind switch
    {
        "official" => "official",
        "self" or "selfOfficial" => "self",
        _ => "none",
    };

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

    /// <summary>
    /// 动画来路标记。最早是尾随半角星号「官方动画*」，符号太小又贴在末尾，
    /// 一眼扫过去看不出和「官方动画」的区别；后来改成前置实心五角星。
    /// 2026-11-12 再改为后缀「官方动画★」：下拉分类里所有条目都以中文开头，
    /// 左边缘齐整，同前缀的普通版与带标记版成对相邻，扫读分组更清楚。
    ///
    /// 用 U+2605「★」而不是 emoji 的 ⭐（U+2B50）：后者走 Segoe UI Emoji 彩色字形，
    /// 尺寸比正文大一截、还会有独立的黄橙色，在 11px 的列表标签里会显得突兀。
    /// ★ 是文字字形，跟正文同族同色，缩放到多少就是多少，跟标签的红绿色也能统一。
    /// </summary>
    private const string Mark = "★";

    /// <summary>特效页的借用标记。自改不再带标记，★ 只剩「借用同名卡」一个含义，
    /// 所以跟动画页共用同一个字符，不再另起空心星。</summary>
    private const string FxMark = "★";

    /// <summary>召唤动画那格的标签文字。
    /// 空=无动画，game=官方动画，shared=借用同名卡通画（带★），yours/wiped=自改动画。
    /// 自改不带★（2026-11-15 跟特效页对齐），★ 只剩「借用」一个含义，来历交给悬停。
    /// </summary>
    public string TagCutin => Cutin switch
    {
        "game" => "官方动画",
        "shared" => "官方动画" + Mark,
        "yours" or "wiped" => "自改动画",
        _ => "无动画",
    };

    /// <summary>
    /// 标签的悬停提示。没有额外信息时给 null（给空串会弹出一个空白提示框）。
    /// 官方动画★ 讲「播的是同名卡那套」，自改动画讲「这套是从哪来的」。
    /// </summary>
    public string? CutinTagTip
    {
        get
        {
            if (Cutin is "yours" or "wiped")
                return CutinOrigin switch
                {
                    "shared" => "来自官方动画" + Mark,
                    "official" => "来自官方动画",
                    _ => "来自无动画",
                };
            if (Cutin == "shared")
                return CutinSrc > 0 && CutinSrcName.Length > 0
                    ? $"借用同名卡播放动画（#{CutinSrc} {CutinSrcName}）"
                    : "借用同名卡播放动画";
            return null;
        }
    }

    private string _cutinOrigin = "";

    /// <summary>改之前那张卡是什么来路：空=原本无动画，official=官方自带动画，
    /// shared=借用同名卡（★）。只用于自改那一行的悬停，不参与标签。</summary>
    public string CutinOrigin
    {
        get => _cutinOrigin;
        set
        {
            if (_cutinOrigin == value) return;
            _cutinOrigin = value;
            Notify(nameof(CutinTagTip));
        }
    }

    /// <summary>发动特效那格的标签文字。
    /// 空=无特效，game=官方特效，shared=借用同名卡特效（带☆），yours/wiped=自改特效。
    ///
    /// 特效页的标记只回答「现在这套是哪来的」：☆ 是借了别卡那一套。自改一律不带标记，
    /// 不管动手之前有没有官方那套 —— 那是来历，交给悬停提示和右侧状态行讲，
    /// 一个符号不承担两件事。
    /// </summary>
    public string TagRunfx => Runfx switch
    {
        "game" => "官方特效",
        "shared" or "borrowed" => "官方特效" + FxMark,
        "yours" or "wiped" => "自改特效",
        _ => "无特效",
    };

    /// <summary>改之前那张卡是什么来路：空=原本无特效，official=官方自有，shared=借用同名卡。
    /// 只用于自改那一行的悬停提示，不参与标签。</summary>
    public string FxOrigin { get; init; } = "";

    /// <summary>特效标签的悬停提示。含义跟动画那格对齐，只是一边讲动画一边讲特效。
    /// 自改那行把来历说清，标签本身不带标记。</summary>
    public string? RunfxTagTip
    {
        get
        {
            if (Runfx is "yours" or "wiped")
                return FxOrigin switch
                {
                    "official" => "来自官方特效",
                    "shared" => "来自官方特效" + FxMark,
                    _ => "来自无特效",
                };
            if (Runfx == "borrowed")
                return RunfxSrc > 0 && RunfxSrcName.Length > 0
                    ? $"借自 #{RunfxSrc} {RunfxSrcName} 的发动特效，点「复原」回到原样"
                    : "借自别卡的官方发动特效，点「复原」回到原样";
            if (Runfx == "shared")
                return RunfxSrc > 0 && RunfxSrcName.Length > 0
                    ? $"借用同名卡播放特效（#{RunfxSrc} {RunfxSrcName}）"
                    : "借用同名卡播放特效";
            return null;
        }
    }

    private bool _fxFromOfficial;

    /// <summary>这张卡原本就有官方特效（覆盖改过之后要标★，复原后清掉）。</summary>
    public bool FxFromOfficial
    {
        get => _fxFromOfficial;
        set
        {
            if (_fxFromOfficial == value) return;
            _fxFromOfficial = value;
            Notify(nameof(TagRunfx), nameof(RunfxTagTip), nameof(FilterRunfx));
        }
    }

    /// <summary>借用的是哪张卡的特效（0 = 没借用）。</summary>
    public int RunfxSrc { get; init; }

    public string RunfxSrcName { get; init; } = "";

    /// <summary>取色用的类别：官方=official(红)，自加=self(绿)，无=none(灰)。</summary>
    public string KindCutin => KindOf(Cutin);

    public string KindRunfx => KindOf(Runfx);

    private string _mode = "cutin";

    /// <summary>
    /// 当前在哪个模式下看这张卡：`cutin` / `runfx`。列表行只显示当前模式的标签，
    /// 因为另一半的「无动画」在这种语境里没有信息量，只会占位置、还容易被读成
    /// 「这张卡什么都没有」。
    /// </summary>
    public string Mode
    {
        get => _mode;
        set
        {
            if (_mode == value) return;
            _mode = value;
            Notify(nameof(TagCurrent), nameof(KindCurrent), nameof(TagTipCurrent));
        }
    }

    public string TagCurrent => _mode == "cutin" ? TagCutin : TagRunfx;

    public string KindCurrent => _mode == "cutin" ? KindCutin : KindRunfx;

    public string? TagTipCurrent => _mode == "cutin" ? CutinTagTip : RunfxTagTip;

    private static string KindOf(string state) => state switch
    {
        "game" or "shared" or "borrowed" => "official",
        "yours" or "wiped" => "self",
        _ => "none",
    };

    /// <summary>
    /// 筛选用的细分类，和 <see cref="KindOf"/> 的区别是「借用」单独占一档。
    /// 取色要按官方/自改两色走，所以 KindXxx 把借用并进官方；筛选要能单独挑出来。
    /// 动画页跟特效页一样四档：官方 / 官方★ / 自改 / 无。自改不再分「原本有没有官方」，
    /// 那是来历，看悬停。
    /// </summary>
    public string FilterCutin => Cutin switch
    {
        "game" => "official",
        "shared" => "shared",
        "yours" or "wiped" => "self",
        _ => "none",
    };

    public string FilterRunfx => Runfx switch
    {
        "game" => "official",
        "shared" or "borrowed" => "shared",
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

    /// <summary>自己有官方动画：改它是覆盖，弹覆盖确认。</summary>
    public bool Official => Cutin == "game";

    /// <summary>
    /// 卡种：monster / spell / trap；空串 = 游戏数据里查不到这个号（CARD_Prop 没有）。
    /// 由后端从游戏 CARD_Prop 读出来，前端不做任何推断。
    /// </summary>
    public string Kind { get; init; } = "";

    /// <summary>通常怪兽 / 衍生物。游戏不会给这类卡做发动特效，特效列表里不列。</summary>
    public bool IsPlain { get; init; }

    /// <summary>
    /// 这张卡在「修改召唤动画」里该不该出现。召唤动画只存在于怪兽卡上，魔法陷阱卡
    /// 再多也只有发动特效，把它们列在召唤动画的列表里、再标一句「无动画」是错的。
    /// 卡种查不出来时按「可以出现」处理：宁可多列一张，也别把真怪兽卡藏了。
    /// </summary>
    public bool InCutinList => Kind != "spell" && Kind != "trap";

    /// <summary>
    /// 走融合召唤演出、没有发动特效的卡（超融合、召唤魔术这批）。
    /// 后端从游戏 FusionEffectSetting 读出来，值是游戏内备注，空串表示没有。
    ///
    /// 这类卡播的是融合召唤那一刻的通用演出，跟「发动特效」是两套系统，
    /// 改了也不会播。所以列表里标「特殊卡」并锁死，不让点。
    /// </summary>
    public string SpecialNote { get; init; } = "";

    public bool IsSpecial => SpecialNote.Length > 0;

    public Visibility SpecialVisibility =>
        IsSpecial ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>特殊卡的悬停说明。一句话说清为什么点不了。</summary>
    public string? SpecialTip =>
        IsSpecial ? "它播的是融合召唤演出，没有可改的发动特效" : null;

    /// <summary>特殊卡不显示状态标签：它压根没有这一维，标「无」会被读成「还没做」。</summary>
    public Visibility StatusTagVisibility =>
        IsSpecial ? Visibility.Collapsed : Visibility.Visible;

    /// <summary>
    /// 这张卡的动画有固定的来源（自己原本就有，或超框登记表指定了底图）。
    /// 这类卡不需要用户挑来源卡，克隆也固定用这个号。
    /// 注意用 CutinSrc 而不是 Cutin：克隆完之后 Cutin 会变成 yours，
    /// 但来源仍然是那个底图，重新生成时还得用它。
    /// </summary>
    /// <summary>来源卡一律可选：官方动画与官方★也用我们自己的来源卡（两页逻辑）。</summary>
    public bool HasFixedSource => false;

    /// <summary>已经有本工具加的动画。</summary>
    public bool HasOurs => Cutin is "yours" or "wiped";

    /// <summary>已经有本工具加的特效，这时才有东西可移除。</summary>
    /// <summary>本工具在这张卡上留了改动，复原按钮才能点。借用也算：借来的那套要能撤。</summary>
    public bool HasOursRunfx => Runfx is "yours" or "wiped" or "borrowed";

    /// <summary>官方自带发动特效，借用或自制都会顶掉它。</summary>
    public bool OfficialRunfx => Runfx == "game";

    /// <summary>
    /// 现在播的是一套官方特效：自己带的（game）或借用同名卡的（shared）。
    /// 超框变体 #22770 属于后者，它的条目和底图 #22692 指着同一个 bundle，
    /// 所以「借用」和「自制」对它都算覆盖，必须弹覆盖确认、后端也要 replace。
    /// </summary>
    public bool HasGameRunfx => Runfx is "game" or "shared";
}
