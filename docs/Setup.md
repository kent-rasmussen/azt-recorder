# Are You Ready to Record Your Wordlist?

You will need a couple things to get started

## Language Code

- Type your language name in the app, and your language should pop up. 
- If it is spoken in multiple countries/regions, select your country/region.
- If you are working on a dialect of another language, or if your language is not yet listed:
	- select the major language, then 
	- select "I'm working on a dialect of this language" to add a variant code to identify your language/dialect.

## Collaboration information

Whether you are starting now, or continuing work, you will want to share it.
Pick GitHub or GitLab and create an account there if you don't have one already.

### GitHub (recommended)

1. Tap "Connect to GitHub"
2. App shows: "Go to github.com/login/device and enter code WDJB-MJHT"
3. Open a browser on any device, enter the code, click "Authorize"
4. App automatically detects authorization and is ready to go

No PATs, no tokens, no git usernames. Just a one-time code entry.

### GitLab

GitLab does not have a device-flow equivalent, so you'll need to
create a personal access token (PAT) once:

1. In GitLab, go to **Preferences → Access Tokens**.
2. Create a token with `read_repository` and `write_repository` scopes.
3. In the recorder's settings, enter your GitLab username and paste
   the token. The daemon stores both for you; you won't be asked again.


