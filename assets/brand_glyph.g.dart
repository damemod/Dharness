// 自有品牌图形，可直接覆盖上游 app/lib/ui/shell/brand_glyph.g.dart。
// 字段与上游 BrandGlyph 接口完全一致，保证 brand_mark.dart 等调用方无需改动。
// 全部 Shader 使用线性渐变，仅依赖 dart:ui，不依赖 dart:typed_data / TileMode。
// 当前为程序绘制的中性几何标识；后续如需更换美术，可替换各 Path 的几何，
// 或由合规的 SVG 母版重新生成，替换时保留全部字段名即可。

import 'dart:ui' as ui;
import 'dart:ui' show Color, Offset, Path, Radius, Rect, RRect, Shader;

/// 自绘标识的坐标空间与上游一致（108x108 画布内居中），保证 brand_mark.dart 的
/// visualCentre = (centroid.dx-box.left)/box.width 等运算得到合理值。
abstract final class BrandGlyph {
  // 白狐头部轮廓的包围盒：与上游 box 同量级，保持宽高 > 0。
  static const box = Rect.fromLTRB(26.75, 28.015, 81.257, 80.004);
  static const aspect = 1.048433;
  static const centroid = Offset(53.5714, 57.7307);
  static const tileBox = Rect.fromLTWH(18, 18, 72, 72);

  // 一条极简「狐首 + 双耳」折线，纯粹为了让代码能编译、能画出中性图形。
  // 全部用相对简单的 lineTo/cubicTo，不依赖任何生成物矩阵。
  static final Path body = Path()
    ..moveTo(34, 60)
    ..cubicTo(34, 46, 44, 36, 54, 36)
    ..cubicTo(64, 36, 74, 46, 74, 60)
    ..cubicTo(74, 72, 64, 80, 54, 80)
    ..cubicTo(44, 80, 34, 72, 34, 60)
    ..close();

  static final Path fin = Path()
    ..moveTo(36, 44)
    ..lineTo(30, 28)
    ..lineTo(46, 38)
    ..close();

  static final Path belly = Path()
    ..moveTo(72, 44)
    ..lineTo(78, 28)
    ..lineTo(62, 38)
    ..close();

  static final Path bubble = Path()
    ..addOval(const Rect.fromLTRB(50, 52, 58, 60));

  static final Path tile = Path()
    ..addRRect(RRect.fromLTRBR(18, 18, 90, 90, const Radius.circular(16)));

  // 9 个 Shader：字段名与上游逐字一致，全部用简单线性渐变（无需 typed_data）。
  static final Shader tileFill = ui.Gradient.linear(
    const Offset(18, 18), const Offset(90, 90),
    const [Color(0xFFE8EEF7), Color(0xFFB9C8E0)], const [0, 1],
  );
  static final Shader tailLight = ui.Gradient.linear(
    const Offset(34, 36), const Offset(74, 80),
    const [Color(0xFFFFFFFF), Color(0xFFD8E2F0)], const [0, 1],
  );
  static final Shader finLight = ui.Gradient.linear(
    const Offset(30, 28), const Offset(46, 44),
    const [Color(0xFFFFFFFF), Color(0xFFC9D8EC)], const [0, 1],
  );
  static final Shader bellyLight = ui.Gradient.linear(
    const Offset(62, 28), const Offset(78, 44),
    const [Color(0xFFFFFFFF), Color(0xFFC9D8EC)], const [0, 1],
  );
  static final Shader bubbleLight = ui.Gradient.linear(
    const Offset(50, 52), const Offset(58, 60),
    const [Color(0xFFFFFFFF), Color(0xFFAFC4E4)], const [0, 1],
  );
  static final Shader tailDark = ui.Gradient.linear(
    const Offset(34, 36), const Offset(74, 80),
    const [Color(0xFF5B6B86), Color(0xFF2C3A55)], const [0, 1],
  );
  static final Shader finDark = ui.Gradient.linear(
    const Offset(30, 28), const Offset(46, 44),
    const [Color(0xFF5B6B86), Color(0xFF2C3A55)], const [0, 1],
  );
  static final Shader bellyDark = ui.Gradient.linear(
    const Offset(62, 28), const Offset(78, 44),
    const [Color(0xFF5B6B86), Color(0xFF2C3A55)], const [0, 1],
  );
  static final Shader bubbleDark = ui.Gradient.linear(
    const Offset(50, 52), const Offset(58, 60),
    const [Color(0xFF7C92B4), Color(0xFF3A4A66)], const [0, 1],
  );
}
