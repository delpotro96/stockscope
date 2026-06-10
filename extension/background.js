/* Clicking the toolbar icon opens InfraScope in the side panel.
 * setPanelBehavior persists, so registering it on install/startup is enough. */
function enableSidePanel() {
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
}
chrome.runtime.onInstalled.addListener(enableSidePanel);
chrome.runtime.onStartup.addListener(enableSidePanel);
