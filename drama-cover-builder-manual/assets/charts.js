// Drama Cover Builder manual — diagram bootstrapping
(function () {
  if (typeof mermaid === 'undefined') return;

  mermaid.initialize({
    startOnLoad: false,
    theme: 'base',
    securityLevel: 'loose',
    fontFamily: '"Microsoft YaHei","PingFang SC","Instrument Sans",sans-serif',
    themeVariables: {
      darkMode: true,
      background: '#15110E',
      primaryColor: '#241E18',
      primaryTextColor: '#F4EDE2',
      primaryBorderColor: '#E3A53B',
      lineColor: '#8A7658',
      secondaryColor: '#1E1914',
      tertiaryColor: '#15110E',
      nodeBorder: '#5A4A35',
      clusterBkg: '#1B1712',
      clusterBorder: '#3A322A',
      edgeLabelBackground: '#15110E',
      fontSize: '14px'
    },
    flowchart: {
      curve: 'basis',
      nodeSpacing: 32,
      rankSpacing: 44,
      useMaxWidth: true
    }
  });

  mermaid.run({ querySelector: '.mermaid' }).catch(function (err) {
    console.error('Mermaid render failed:', err);
  });
})();
