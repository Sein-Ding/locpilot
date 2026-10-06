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
            .overlay(alignment: .topTrailing) {
                ControlsCluster().padding(.top, 12).padding(.trailing, 14)
            }
            .overlay(alignment: .bottomLeading) {
                CoordinateReadout().padding(.leading, 16).padding(.bottom, 14)
            }
            .overlay(alignment: .top) {
                if let message = state.lastMessage { MessagePill(text: message).padding(.top, 58) }
            }
        }
        .ignoresSafeArea()
    }
}

/// 左下角读数：无背景、低对比度，不抢地图。
private struct CoordinateReadout: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        Text(state.position.map { AppState.format(lat: $0.lat, lon: $0.lon) } ?? "—")
            .font(.system(size: 11, design: .monospaced))
            .foregroundStyle(.primary)
            .shadow(color: .white.opacity(0.75), radius: 2, y: 1)
            .padding(.horizontal, 2)
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
