using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;

namespace MDStudio.Controls;

/// <summary>
/// 滚轮不切换选项的下拉框。
///
/// 原生 ComboBox 在鼠标悬停时滚动滚轮会直接改选中项，参数区在 ScrollViewer 里，
/// 很容易在滚动页面时误改「边框效果」。这里覆盖掉滚轮处理：
/// 不调用基类（基类才会用滚轮改选中），也不把事件标记为已处理，
/// 让它继续冒泡给外层 ScrollViewer，页面滚动照旧。
/// </summary>
public sealed class WheelSafeComboBox : ComboBox
{
    protected override void OnPointerWheelChanged(PointerRoutedEventArgs e)
    {
        // 故意留空，见类注释。
    }
}
