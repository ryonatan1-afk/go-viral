const raw = $input.first().json;
const update = raw.body ?? raw;
const callback = update.callback_query;

if (!callback) {
  throw new Error('No callback_query. Raw: ' + JSON.stringify(raw).slice(0, 500));
}

const [action, dateKey, indexStr] = callback.data.split('|');
const resumeSidecar = `/output/content/resume_${dateKey}.txt`;

const { execSync } = require('child_process');
let resumeUrl;
try {
  resumeUrl = execSync(`cat ${resumeSidecar}`).toString().trim();
} catch(e) {
  throw new Error('Could not read resume URL: ' + e.message);
}

execSync(`curl -s -X POST https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery -d callback_query_id=${callback.id} -d text="Got it!"`);

return [{ json: { resume_url: resumeUrl, action, date: dateKey, band_index: indexStr ? parseInt(indexStr) : null } }];
