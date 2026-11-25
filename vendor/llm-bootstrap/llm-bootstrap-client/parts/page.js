/* The bootstrap's own page, made with the kit's own calls - the calls an
   application's page makes (the kit is the script before this one). Its bar
   is a container docked to the top of the window; its instruments are
   behind the Tools door, in the rest of it. */

llmKit.container('top', {
  name: 'bar',
  slots: { start: ['workspace'], middle: ['queue'], end: ['log', 'customize'] },
  shed: ['workspace', 'queue', 'log'],
});
llmKit.place('tools', document.getElementById('app'));
