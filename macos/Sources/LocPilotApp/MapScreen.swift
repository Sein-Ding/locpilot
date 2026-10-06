import LocPilotKit
import MapKit
import SwiftUI

/// 唯一主界面：Apple 地图铺满窗口，点击即改定位，右上控件簇，左下坐标读数。
struct MapScreen: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        MapReader { proxy in
            Map(position: $state.camera, interactionModes: [.pan, .zoom]) {
                if let marker = state.marker {
                    // anchor: .bottom = 组件的底边中心（也就是针尖）钉在坐标上，
                    // 所有缩放/旋转都以它为轴，保证针尖不动（规格 §3 / §13）。
                    Annotation("", coordinate: CLLocationCoordinate2D(latitude: marker.lat, longitude: marker.lon), anchor: .bottom) {
                        MapLocationPin(animator: state.pin, label: marker.label)
                    }
                }
            }
            .mapStyle(.standard(elevation: .flat, pointsOfInterest: .all))
            .mapControlVisibility(.hidden)
            // 官方方式拿当前可见区域：MapCameraPosition 是 struct，取不回 region
            .onMapCameraChange(frequency: .continuous) { context in
                state.updateVisibleRegion(context.region)
            }
            .onTapGesture { point in
                // MapReader 把屏幕点换算成地理坐标：这是 SwiftUI Map 里取得点击位置的官方方式。
                // 注意：这里不直接改定位 —— 先落针（带动效）并显示地址，落稳后才下发。
                guard let coordinate = proxy.convert(point, from: .local) else { return }
                Task { await state.selectLocation(lat: coordinate.latitude, lon: coordinate.longitude) }
            }
            // 顶部：先铺悬停遮罩，再铺拖拽热区（遮罩 allowsHitTesting=false，不会挡拖拽）。
            // 都必须在控件簇之前叠加 —— 后叠加的控件簇层级更高，按钮照常可点。
            .overlay(alignment: .top) {
                if state.titlebarHovered { TitlebarVeil() }
            }
            .overlay(alignment: .top) {
                TitlebarDragArea { state.setTitlebarHovered($0) }
                    .frame(height: TitlebarVeil.height)   // 与悬停条同高：整条都能拖窗口
            }
            // 设备图标中心与红绿灯中心对齐（都在距顶 22pt），悬停条以这条线上下等长；
            // 下面的定位/缩放胶囊自然落在悬停条之外，不侵占它。
            .overlay(alignment: .topTrailing) {
                ControlsCluster()
                    .padding(.top, ControlsCluster.topInset)
                    .padding(.trailing, ControlsCluster.trailingInset)
            }
            .overlay(alignment: .bottomLeading) {
                // 占位不显示，所以这里也不留下多余的空间（无定位时 overlay 自然为空）
                CoordinateReadout().padding(.leading, 16).padding(.bottom, 14)
            }
            .overlay(alignment: .top) {
                if let message = state.lastMessage { MessagePill(text: message).padding(.top, 58) }
            }
        }
        .ignoresSafeArea()
    }
}

/// 左下角坐标读数：只在"确实有定位"时显示。
/// 没有定位时**什么都不画** —— 之前用 "—" 占位，看起来就是地图左下角多了一根莫名其妙的横杠。
private struct CoordinateReadout: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        if let position = state.position {
            Text(AppState.format(lat: position.lat, lon: position.lon))
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.primary)
                .shadow(color: .white.opacity(0.75), radius: 2, y: 1)
                .padding(.horizontal, 2)
        }
    }
}

/// 顶部临时提示（2.4s 自动消失）。
private struct MessagePill: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 12.5))
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .modifier(GlassChrome(shape: Capsule()))
            .transition(.opacity)
    }
}
