using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Xaml.Media;

namespace MDStudio.Services;

/// <summary>
/// 三页共用的对话框。卡图、动画、特效的生成与复原确认框，以及成功／失败结果框
/// 都走这里，样式只有一份。
///
/// 排版照 Fluent 的 ContentDialog 规范：标题左对齐（20px Semibold），正文左对齐，
/// 按钮整块靠右、间距 8px、宽度随文字自适应。弹窗整体由 ContentDialog 按 XamlRoot 居中。
///
/// 为什么不用 ContentDialog 自带的 PrimaryButtonText / CloseButtonText：
/// 模板的 CommandSpace 把按钮放进弹性列并强制 Stretch，按钮会被拉成两大块，
/// 而且宽度改不动（Opened 里改完还会被模板覆盖，实测 CloseButton 稳定停在 216px）。
/// 所以这里只留 ContentDialog 的遮罩、居中与进出动画，标题正文按钮全部自绘。
///
/// 交互：主按钮＋取消；Esc 等同取消；回车不会误确认。
/// 这里只负责对话框本身，不碰遮罩、底部状态条或进度条。任务还没结束时不要弹
/// 结果框，否则用户是隔着遮罩看到「完成」的（见 AnimPage.RunJobAsync）。
/// </summary>
public static class Dialogs
{
    /// <summary>正文与标题统一宽度，弹窗因此宽度一致。
    /// 不要给 ContentDialog 自身设 MinWidth=MaxWidth：那会破坏它按 XamlRoot
    /// 居中的布局，实测弹窗会跑到窗口左侧约 18% 的位置。</summary>
    private const double DialogWidth = 440;

    /// <summary>按钮最小宽度。Fluent 的对话框按钮宽度由文字决定，
    /// 只给一个下限，免得「取消」这类两字按钮窄成一小块。</summary>
    private const double ButtonMinWidth = 96;

    /// <summary>切到别的页面时本页 XamlRoot 会变 null，退一步用窗口根元素。</summary>
    public static XamlRoot? Root(XamlRoot? pageRoot)
        => pageRoot ?? (App.MainWindow?.Content as FrameworkElement)?.XamlRoot;

    private static Style? ResStyle(string key)
        => Application.Current.Resources.TryGetValue(key, out var v) && v is Style s ? s : null;

    private static Brush? ResBrush(string key)
        => Application.Current.Resources.TryGetValue(key, out var v) && v is Brush b ? b : null;

    /// <summary>确认框。返回是否点了主按钮。
    ///
    /// cardName / cardMeta 是操作对象那一行：卡名原样传（游戏卡名里可能自带
    /// 「S:」「★」这类前缀，都是名字的一部分，不要裁剪），cardMeta 放卡号和卡种。
    /// note 是安全提示，单独放进底部的浅色条，不再混在说明正文里。</summary>
    public static async Task<bool> ConfirmAsync(XamlRoot? root, string title, string body,
                                               string primaryText,
                                               string? cardName = null, string? cardMeta = null,
                                               string? note = null)
    {
        var xr = Root(root);
        if (xr is null)
        {
            App.AddLog("ERROR", $"确认框未显示：{title} / {body}");
            return false;
        }

        var result = new TaskCompletionSource<bool>();
        var dlg = Build(xr, title, body, danger: false, dryNote: true,
                        cardName, cardMeta, note,
                        out var primary, out var cancel);

        primary.Content = primaryText;
        primary.Style = ResStyle("AccentButtonStyle");
        primary.Click += (_, _) => { result.TrySetResult(true); dlg.Hide(); };

        cancel.Content = "取消";
        cancel.Click += (_, _) => { result.TrySetResult(false); dlg.Hide(); };

        await ShowAsync(dlg, title, body);
        // 按 Esc 或点遮罩关掉时 ShowAsync 返回，这里兜底成「取消」
        return result.Task.IsCompleted && result.Task.Result;
    }

    /// <summary>结果框与纯提示。成功／失败都用单按钮，失败标题用警示色。
    /// 预演说明由调用方的文案自己写，这里不自动补：结果框的「流程跑通了」已经说了，
    /// 而「还没选素材」这类纯提示跟预演无关。</summary>
    public static async Task InfoAsync(XamlRoot? root, string title, string body,
                                      bool danger = false,
                                      string? cardName = null, string? cardMeta = null,
                                      string? note = null)
    {
        var xr = Root(root);
        if (xr is null)
        {
            App.AddLog("ERROR", $"{title}：{body}");
            return;
        }

        var dlg = Build(xr, title, body, danger, dryNote: false,
                        cardName, cardMeta, note,
                        out var primary, out var cancel);
        primary.Visibility = Visibility.Collapsed;

        cancel.Content = "知道了";
        cancel.Style = ResStyle("AccentButtonStyle");
        cancel.Click += (_, _) => dlg.Hide();

        await ShowAsync(dlg, title, body);
    }

    /// <summary>
    /// 拼出弹窗内容。ContentDialog 只提供遮罩、居中与动画，界面全在这里画。
    /// 主按钮与次按钮已放进同一行，调用方决定文字、样式和显隐。
    ///
    /// 结构自上而下：标题 → 操作对象（卡名＋编号） → 说明 → 安全提示条 → 按钮。
    /// 三段信息三种视觉待遇，扫一眼就知道「在改哪张卡」「做了什么」「还能不能退回来」。
    /// </summary>
    private static ContentDialog Build(XamlRoot xr, string title, string body, bool danger,
                                       bool dryNote, string? cardName, string? cardMeta,
                                       string? note,
                                       out Button primary, out Button cancel)
    {
        var panel = new StackPanel { Width = DialogWidth };

        var titleBlock = new TextBlock
        {
            Text = title,
            FontSize = 20,
            FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 12),
        };
        if (danger && ResBrush("SystemFillColorCriticalBrush") is { } warn)
            titleBlock.Foreground = warn;
        panel.Children.Add(titleBlock);

        // 操作对象：卡名加粗，编号与卡种跟在后面用小字。卡名原样显示，不裁剪任何前缀。
        if (!string.IsNullOrWhiteSpace(cardName))
        {
            var nameRow = new TextBlock
            {
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 10),
            };
            nameRow.Inlines.Add(new Run
            {
                Text = cardName,
                FontSize = 15,
                FontWeight = FontWeights.SemiBold,
            });
            if (!string.IsNullOrWhiteSpace(cardMeta))
            {
                var meta = new Run { Text = "  " + cardMeta, FontSize = 12.5 };
                if (ResBrush("TextFillColorSecondaryBrush") is { } dim) meta.Foreground = dim;
                nameRow.Inlines.Add(meta);
            }
            panel.Children.Add(nameRow);
        }

        if (dryNote && App.DryRun)
            body += "\n预演模式：不会写游戏文件。";
        panel.Children.Add(new TextBlock
        {
            Text = body,
            FontSize = 14,
            TextWrapping = TextWrapping.Wrap,
            IsTextSelectionEnabled = true,
        });

        // 安全提示：左侧一道强调色竖条，浅色底，跟说明正文拉开距离。
        // 「之后点复原可以还原」「这一步不能撤销」这类信息混在正文里等于没说。
        if (!string.IsNullOrWhiteSpace(note))
        {
            var noteText = new TextBlock
            {
                Text = note,
                FontSize = 13,
                TextWrapping = TextWrapping.Wrap,
            };
            var noteBox = new Border
            {
                Padding = new Thickness(12, 8, 12, 8),
                Margin = new Thickness(0, 14, 0, 0),
                CornerRadius = new CornerRadius(4),
                BorderThickness = new Thickness(3, 0, 0, 0),
                Child = noteText,
            };
            if (ResBrush("SystemFillColorAttentionBackgroundBrush") is { } noteBg)
                noteBox.Background = noteBg;
            if (ResBrush("SystemFillColorAttentionBrush") is { } noteEdge)
                noteBox.BorderBrush = noteEdge;
            else
                noteText.Opacity = 0.85;
            panel.Children.Add(noteBox);
        }

        primary = new Button { MinWidth = ButtonMinWidth };
        cancel = new Button { MinWidth = ButtonMinWidth };
        var cancelButton = cancel;

        // Fluent 的按钮区：整块靠右、间距 8，主按钮在左、次按钮在右
        panel.Children.Add(new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Spacing = 8,
            Margin = new Thickness(0, 24, 0, 0),
            Children = { primary, cancel },
        });

        var dlg = new ContentDialog
        {
            Content = Scroll(xr, panel),
            XamlRoot = xr,
        };
        // 弹窗默认把焦点给第一个可聚焦元素，也就是主按钮，回车会直接确认掉一次
        // 不可撤销的写入。把焦点先放到「取消／知道了」上，回车才是安全的。
        dlg.Opened += (_, _) => cancelButton.Focus(FocusState.Programmatic);
        return dlg;
    }

    // ------------------------------------------------------------------ 勾选列表
    /// <summary>勾选列表里的一行：一张卡的一种改动。</summary>
    public sealed record PickItem(int Card, string Name, string Kind, int Files)
    {
        public string Key => $"{Card}|{Kind}";
    }

    /// <summary>
    /// 勾选列表式确认框。返回勾中的行；点取消或 Esc 返回 null。
    ///
    /// 为什么单独一个方法：快捷复原要按行挑，纯文字确认框装不下。三页通用的
    /// ConfirmAsync 只管「确定/取消」，塞一个列表进去会把它的骨架撑变形。
    /// 版式沿用同一套：标题、说明、提示条、按钮都靠右。
    /// </summary>
    public static async Task<List<PickItem>?> PickAsync(XamlRoot? root, string title, string body,
                                                       string note, IReadOnlyList<PickItem> rows,
                                                       string primaryText,
                                                       Func<string, string>? kindLabel = null)
    {
        var xr = Root(root);
        if (xr is null)
        {
            App.AddLog("ERROR", $"勾选列表未显示：{title}");
            return null;
        }

        var label = kindLabel ?? (k => k);
        var panel = new StackPanel { Width = DialogWidth };
        panel.Children.Add(new TextBlock
        {
            Text = title,
            FontSize = 20,
            FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 12),
        });
        panel.Children.Add(new TextBlock
        {
            Text = body,
            FontSize = 14,
            TextWrapping = TextWrapping.Wrap,
        });

        if (!string.IsNullOrWhiteSpace(note))
        {
            var noteText = new TextBlock
            {
                Text = note,
                FontSize = 13,
                TextWrapping = TextWrapping.Wrap,
            };
            var noteBox = new Border
            {
                Padding = new Thickness(12, 8, 12, 8),
                Margin = new Thickness(0, 14, 0, 0),
                CornerRadius = new CornerRadius(4),
                BorderThickness = new Thickness(3, 0, 0, 0),
                Child = noteText,
            };
            if (ResBrush("SystemFillColorAttentionBackgroundBrush") is { } noteBg)
                noteBox.Background = noteBg;
            if (ResBrush("SystemFillColorAttentionBrush") is { } noteEdge)
                noteBox.BorderBrush = noteEdge;
            else
                noteText.Opacity = 0.85;
            panel.Children.Add(noteBox);
        }

        // 全选在前，逐行在后。列表本身限高滚动，卡多也不会把按钮顶出窗口。
        var checks = new List<(CheckBox Box, PickItem Item)>();
        var list = new StackPanel { Spacing = 2, Margin = new Thickness(0, 14, 0, 0) };
        var all = new CheckBox { Content = $"全选（{rows.Count} 项）", IsChecked = true };
        list.Children.Add(all);
        foreach (var item in rows)
        {
            var text = new TextBlock { TextWrapping = TextWrapping.Wrap };
            text.Inlines.Add(new Run
            {
                Text = string.IsNullOrWhiteSpace(item.Name) ? $"#{item.Card}" : $"#{item.Card} {item.Name}",
                FontSize = 14,
            });
            var meta = new Run { Text = $"　{label(item.Kind)} · {item.Files} 个文件", FontSize = 12.5 };
            if (ResBrush("TextFillColorSecondaryBrush") is { } dim) meta.Foreground = dim;
            text.Inlines.Add(meta);
            var box = new CheckBox { Content = text, IsChecked = true, MinWidth = 0 };
            checks.Add((box, item));
            list.Children.Add(box);
        }
        panel.Children.Add(new ScrollViewer
        {
            Content = list,
            MaxHeight = 260,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        });

        var primary = new Button { MinWidth = ButtonMinWidth };
        var cancel = new Button { MinWidth = ButtonMinWidth };
        panel.Children.Add(new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Spacing = 8,
            Margin = new Thickness(0, 20, 0, 0),
            Children = { primary, cancel },
        });

        var result = new TaskCompletionSource<List<PickItem>?>();

        // 勾选数直接写在按钮上，用户不用自己数还剩几行。
        void Sync()
        {
            int n = checks.Count(c => c.Box.IsChecked == true);
            primary.Content = n > 0 ? $"{primaryText}（{n}）" : primaryText;
            primary.IsEnabled = n > 0;
        }

        foreach (var (box, _) in checks)
            box.Checked += (_, _) => { Sync(); if (all.IsChecked != true) all.IsChecked = null; };
        foreach (var (box, _) in checks)
            box.Unchecked += (_, _) =>
            {
                Sync();
                if (checks.All(c => c.Box.IsChecked == true)) all.IsChecked = true;
                else if (checks.All(c => c.Box.IsChecked != true)) all.IsChecked = false;
                else all.IsChecked = null;
            };
        all.Checked += (_, _) => { foreach (var (b, _) in checks) b.IsChecked = true; Sync(); };
        all.Unchecked += (_, _) => { foreach (var (b, _) in checks) b.IsChecked = false; Sync(); };
        all.IsThreeState = true;

        primary.Content = primaryText;
        primary.Style = ResStyle("AccentButtonStyle");
        primary.Click += (_, _) =>
        {
            result.TrySetResult(checks.Where(c => c.Box.IsChecked == true).Select(c => c.Item).ToList());
        };
        cancel.Content = "取消";
        cancel.Click += (_, _) => { result.TrySetResult(null); };

        var dlg = new ContentDialog { Content = Scroll(xr, panel), XamlRoot = xr };
        primary.Click += (_, _) => dlg.Hide();
        cancel.Click += (_, _) => dlg.Hide();
        // 焦点先给取消：回车不该把一批改动撤掉。
        dlg.Opened += (_, _) => cancel.Focus(FocusState.Programmatic);
        Sync();
        await ShowAsync(dlg, title, body);
        return result.Task.IsCompleted ? result.Task.Result : null;
    }

    /// <summary>
    /// 给内容套一层限高的滚动区。异常信息里带 traceback 时正文能长到几千像素，
    /// 弹窗会顶出屏幕，底下的按钮点不到，界面就卡在那一屏。
    /// 高度按窗口高度折算，窗口小也不会连按钮一起挤出去。
    /// </summary>
    private static ScrollViewer Scroll(XamlRoot xr, UIElement content)
    {
        var available = xr.Size.Height;
        var max = double.IsNaN(available) || available < 200
            ? 420
            : Math.Max(200, available * 0.62);
        return new ScrollViewer
        {
            Content = content,
            MaxHeight = max,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
    }

    /// <summary>
    /// ShowAsync 包一层：同一时刻只能有一个 ContentDialog，前一个还没退干净时
    /// 这里的 ShowAsync 会直接抛。以前吞掉就当「用户点了取消」，于是一次缺组件
    /// 提示就这么消失了（2026-11-16 实测：日志里有内容，屏幕上什么都没有，操作还中止了）。
    ///
    /// 现在重试三次，每次让出 250ms 给上一个对话框收尾。三次都不行才认输并记日志，
    /// 那种情况基本只剩「真有一个对话框卡在屏幕上没关」，用户看得见，不会误以为没反应。
    /// </summary>
    private static async Task ShowAsync(ContentDialog dlg, string title, string body)
    {
        for (int i = 0; ; i++)
        {
            try
            {
                await dlg.ShowAsync();
                return;
            }
            catch (Exception ex)
            {
                if (i >= 2)
                {
                    App.AddLog("ERROR", $"{title}：{body}（对话框没能显示：{ex.Message}）");
                    return;
                }
                await Task.Delay(250);
            }
        }
    }
}
