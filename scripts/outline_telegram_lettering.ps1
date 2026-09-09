# Optional authoring step on Windows; ordinary SVG/PNG export uses the checked-in
# outlines and does not require this font or System.Drawing.
Add-Type -AssemblyName System.Drawing
$copy = [ordered]@{
    brand = @('Reminder Bot', 38, 'Bold')
    headline1 = @('Помнить всё', 86, 'Bold')
    headline2 = @('— проще.', 86, 'Bold')
    subtitle1 = @('Умная напоминалка', 38, 'Regular')
    subtitle2 = @('без сложных форм', 38, 'Regular')
    voice = @('Текстом и голосом', 28, 'Regular')
    request1 = @('Напомни завтра в 9', 43, 'Regular')
    request2 = @('позвонить маме', 43, 'Regular')
    time = @('Завтра, 09:00', 38, 'Bold')
    task = @('Позвонить маме', 45, 'Bold')
    confirm = @('Создать', 32, 'Bold')
    footer = @('Напиши или скажи → подтверди → получи напоминание', 29, 'Regular')
}
$outlines = [ordered]@{}
$fontFamily = [System.Drawing.FontFamily]::new('Segoe UI')
foreach ($key in $copy.Keys) {
    $entry = $copy[$key]
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $path.AddString($entry[0], $fontFamily, [int][System.Drawing.FontStyle]::$($entry[2]),
        [single]$entry[1], [System.Drawing.PointF]::new(0, 0),
        [System.Drawing.StringFormat]::GenericTypographic)
    $path.Flatten($null, 0.12)
    $polygons = [System.Collections.Generic.List[object]]::new()
    $polygon = [System.Collections.Generic.List[object]]::new()
    for ($i = 0; $i -lt $path.PointCount; $i++) {
        $point = $path.PathPoints[$i]
        $polygon.Add(@([math]::Round($point.X, 3), [math]::Round($point.Y, 3)))
        if (($path.PathTypes[$i] -band 128) -ne 0) {
            $polygons.Add($polygon.ToArray())
            $polygon = [System.Collections.Generic.List[object]]::new()
        }
    }
    $outlines[$key] = $polygons.ToArray()
    $path.Dispose()
}
$fontFamily.Dispose()
$target = Join-Path $PSScriptRoot '../assets/telegram/lettering.json'
[System.IO.File]::WriteAllText($target, ($outlines | ConvertTo-Json -Depth 8 -Compress),
    [System.Text.UTF8Encoding]::new($false))
