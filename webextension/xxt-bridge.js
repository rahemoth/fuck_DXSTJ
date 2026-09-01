// 极小 service worker:唯一作用是让插件在调试目标列表(/json)中可见,
// 主程序据此确认 --load-extension 装载成功。注册事件监听使其随浏览器启动唤醒。
chrome.runtime.onStartup.addListener(() => {});
chrome.runtime.onInstalled.addListener(() => {});
