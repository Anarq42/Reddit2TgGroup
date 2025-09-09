Reddit to Telegram BotThis is a Python script that automates the process of fetching new, media-rich posts from specified Reddit subreddits and forwarding them to different topics within a Telegram group. The bot is designed to run continuously, checking for new content every 30 minutes, and uses the official Reddit and Telegram APIs for reliable operation.FeaturesMulti-Subreddit Support: Easily configure the bot to monitor multiple subreddits simultaneously.Topic-Specific Delivery: Each subreddit's new posts can be directed to a specific topic within your Telegram group.Media-Only Filtering: The bot only processes posts that contain an image, video, or a gallery.Detailed Posts: Each message sent to Telegram includes the post's title, author, a direct link, and the top 3 comments.Duplicate Prevention: The script tracks posts to ensure the same content is not sent multiple times.Robust Error Handling: Includes logging and error handling to ensure the bot can gracefully manage API issues or network problems.PrerequisitesTo run this bot, you will need the following:Python 3.xA Reddit Account and API Credentials: You need to register a "script" application on Reddit's App Preferences page. Make a note of your client_id, client_secret, username, and password.A Telegram Account and Bot Token: Create a new bot by talking to the @BotFather on Telegram. The BotFather will provide you with a unique bot token.A Telegram Group and Topic IDs: Add your new bot to a Telegram group and find the group's ID (it starts with -100). If your group uses topics, you'll need the unique topic ID for each topic you wish to send posts to.SetupClone the Repositorygit clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
cd your-repo-name
Install Required LibrariesThe bot uses a few third-party Python libraries. Install them using the provided requirements.txt file.pip install -r requirements.txt
Configure Environment VariablesFor security, the bot's credentials are read from environment variables. Create a file named .env in the project root to store them (this file should not be committed to version control).# .env
REDDIT_CLIENT_ID='your_reddit_client_id'
REDDIT_CLIENT_SECRET='your_reddit_client_secret'
REDDIT_USERNAME='your_reddit_username'
REDDIT_PASSWORD='your_reddit_password'
TELEGRAM_BOT_TOKEN='your_telegram_bot_token'
TELEGRAM_GROUP_ID='-100xxxxxxxxxx'
Note: On Railway, you'll add these variables directly in the platform's settings.Create the Subreddit Configuration FileCreate a file named subreddits.db in the project root. This file tells the bot which subreddits to monitor and which Telegram topic to send posts to. Each line should contain a subreddit name and its corresponding topic ID, separated by a comma.# subreddits.db
# subreddit, topic_id
unixporn, 123
NatureIsFuckingLit, 456
aww, 789
UsageRunning LocallyTo run the bot on your local machine, you must first load the environment variables from the .env file and then run the script.python main.py
Deployment on RailwayTo deploy the bot on a platform like Railway, you'll need to define a Procfile and ensure you have a requirements.txt file.ProcfileCreate a file named Procfile in the project root. This tells Railway how to start your application.worker: python main.py
Requirements.txtEnsure your requirements.txt file contains all necessary libraries:praw
python-telegram-bot
requests
The Railway platform will automatically install these dependencies and run the command in the Procfile, starting your bot as a worker process.
