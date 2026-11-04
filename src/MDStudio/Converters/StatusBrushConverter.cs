using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;

namespace MDStudio.Converters;

/// <summary>把状态类别映射成 Fluent 的语义色，而不是自己写死色号。
/// official=官方(红)，self=自改(绿)，none=无(灰)。</summary>
public sealed class StatusBrushConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language)
    {
        var key = (value as string) switch
        {
            "official" => "SystemFillColorCriticalBrush",
            "self" => "SystemFillColorSuccessBrush",
            _ => "TextFillColorSecondaryBrush",
        };
        return Application.Current.Resources.TryGetValue(key, out var v) && v is Brush b
            ? b
            : new SolidColorBrush();
    }

    public object ConvertBack(object value, Type targetType, object parameter, string language)
        => throw new NotSupportedException();
}
