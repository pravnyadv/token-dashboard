// Keeps the dashboard running at http://tally.test (Herd proxy -> this port).
//   pm2 start ecosystem.config.cjs   -> start
//   pm2 logs tally                   -> logs
//   herd proxy tally http://localhost:5330   -> the .test name (once)
// The server rescans every 30 s; the page also triggers a scan when it opens.
module.exports = {
  apps: [
    {
      name: 'tally',
      script: 'cli.py',
      args: 'dashboard --no-open',
      interpreter: '/opt/homebrew/bin/python3', // symlink that follows brew upgrades
      cwd: __dirname,
      env: { HOST: '127.0.0.1', PORT: '5330' }
    }
  ]
};
