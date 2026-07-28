"""
================================================================================
📺 YOUTUBE COMMENT ANALYZER - AI-Powered Comment Tagging System
================================================================================

DESCRIPTION:
    This script automatically analyzes your YouTube channel comments using AI.
    It tags comments with categories like: urgent, testimonial, question, etc.
    Runs daily to process only NEW comments and sends email alerts for:
      - 🚨 Urgent comments (need immediate attention)
      - ⭐ Testimonial-worthy comments (positive praise, success stories)
      - 📊 Daily summary email (overview of all analyzed comments)

SECRETS NEEDED (add these in PyRunner's Secret Manager):
    - YOUTUBE_API_KEY  : Your YouTube Data API v3 key (from Google Cloud Console)
    - OPENAI_API_KEY   : Your OpenAI API key (for GPT-based classification)
    - RESEND_API_KEY   : Your Resend API key (for sending urgent alerts)

PACKAGES TO INSTALL:
    - google-api-python-client
    - openai
    - resend

HOW IT WORKS:
    1. Fetches all videos from your YouTube channel
    2. Retrieves comments from each video (only new ones after last run)
    3. Uses OpenAI to classify each comment into categories
    4. Stores analyzed comments in DataStore
    5. Sends email alerts for urgent comments
    6. Sends email alerts for testimonial-worthy comments
    7. Sends a daily summary email (only if new comments were found)
    8. Prints a daily summary report

================================================================================
"""

import os
import json
from datetime import datetime, timezone
from googleapiclient.discovery import build
from openai import OpenAI
import resend

# Import PyRunner's DataStore for persistent storage
from pyrunner_datastore import DataStore

# ==============================================================================
# ⚙️ CONFIGURATION - Edit these variables as needed
# ==============================================================================

# Your YouTube Channel ID (find it in YouTube Studio > Settings > Channel > Advanced)
CHANNEL_ID = "UCl4nXWTkPOqmKlEmIN5_TJQ"

# Starting date - only analyze comments from this date onwards (YYYY-MM-DD format)
# After first run, the script will only process NEW comments automatically
START_DATE = "2026-01-25"

# Email to receive alerts and summaries
ALERT_EMAIL = "hasan@learnwithhasan.com"

# Sender email (must be verified in Resend)
SENDER_EMAIL = "alerts@learnwithhasan.com"

# ==============================================================================
# 🏷️ COMMENT CATEGORIES - The AI will classify comments into these tags
# ==============================================================================

COMMENT_TAGS = {
    "urgent": "Needs immediate response - frustrated users, serious complaints, or time-sensitive issues",
    "testimonial": "Positive praise that could be featured - success stories, course results, thank you messages",
    "negative": "Criticism or complaints that aren't urgent but worth monitoring",
    "question": "Asking a question that needs an answer",
    "content_idea": "Suggestions for future videos or content requests",
    "brand_mention": "Mentions other brands, tools, products, or competitors",
    "collaboration": "Partnership requests, collab proposals, or business inquiries",
    "spam": "Irrelevant content, self-promotion, or bot-like messages",
    "positive": "General positive feedback, encouragement, or supportive messages",
    "feature_request": "Requests for specific tutorials, features, or topics to cover"
}

# ==============================================================================
# 🔐 LOAD SECRETS FROM ENVIRONMENT
# ==============================================================================

print("=" * 60)
print("🚀 YOUTUBE COMMENT ANALYZER - Starting...")
print("=" * 60)

# Load API keys from PyRunner's Secret Manager
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY')

# Validate that all required secrets are present
missing_secrets = []
if not YOUTUBE_API_KEY:
    missing_secrets.append("YOUTUBE_API_KEY")
if not OPENAI_API_KEY:
    missing_secrets.append("OPENAI_API_KEY")
if not RESEND_API_KEY:
    missing_secrets.append("RESEND_API_KEY")

if missing_secrets:
    print(f"❌ ERROR: Missing required secrets: {', '.join(missing_secrets)}")
    print("   Please add them in PyRunner's Secret Manager")
    exit(1)

print("✅ All secrets loaded successfully")

# ==============================================================================
# 📦 INITIALIZE SERVICES
# ==============================================================================

# Initialize YouTube API client
youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
print("✅ YouTube API client initialized")

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)
print("✅ OpenAI client initialized")

# Initialize Resend for email alerts
resend.api_key = RESEND_API_KEY
print("✅ Resend email client initialized")

# Initialize DataStore for persistent storage
store = DataStore("youtube_comments")
print("✅ DataStore initialized")

# ==============================================================================
# 📅 DETERMINE DATE RANGE FOR ANALYSIS
# ==============================================================================

def get_last_run_date():
    """
    Get the timestamp of the last successful run.
    If never run before, use the START_DATE from configuration.
    """
    last_run = store.get("_last_run_timestamp", default=None)
    
    if last_run:
        print(f"📅 Last run was at: {last_run}")
        return last_run
    else:
        # First run - use the configured START_DATE
        start_timestamp = f"{START_DATE}T00:00:00Z"
        print(f"📅 First run! Analyzing comments from: {START_DATE}")
        return start_timestamp


def save_run_timestamp():
    """
    Save the current timestamp as the last run time.
    This ensures next run only processes newer comments.
    """
    current_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    store["_last_run_timestamp"] = current_time
    print(f"💾 Saved run timestamp: {current_time}")

# ==============================================================================
# 📺 YOUTUBE API FUNCTIONS
# ==============================================================================

def get_channel_videos():
    """
    Fetch all video IDs from the channel.
    Returns a list of dictionaries with video info.
    """
    print("\n📺 Fetching videos from channel...")
    
    videos = []
    next_page_token = None
    
    # First, get the uploads playlist ID
    channel_response = youtube.channels().list(
        part='contentDetails',
        id=CHANNEL_ID
    ).execute()
    
    if not channel_response.get('items'):
        print(f"❌ ERROR: Channel not found with ID: {CHANNEL_ID}")
        return []
    
    uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
    print(f"   Found uploads playlist: {uploads_playlist_id}")
    
    # Fetch all videos from the uploads playlist
    while True:
        playlist_response = youtube.playlistItems().list(
            part='snippet',
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=next_page_token
        ).execute()
        
        for item in playlist_response.get('items', []):
            video_info = {
                'video_id': item['snippet']['resourceId']['videoId'],
                'title': item['snippet']['title'],
                'published_at': item['snippet']['publishedAt']
            }
            videos.append(video_info)
        
        next_page_token = playlist_response.get('nextPageToken')
        if not next_page_token:
            break
    
    print(f"   ✅ Found {len(videos)} videos on channel")
    return videos


def get_video_comments(video_id, video_title, after_date):
    """
    Fetch all comments for a specific video that were posted after a certain date.
    Returns a list of comment dictionaries.
    """
    comments = []
    next_page_token = None
    
    try:
        while True:
            # Fetch comment threads for the video
            response = youtube.commentThreads().list(
                part='snippet',
                videoId=video_id,
                maxResults=100,
                pageToken=next_page_token,
                order='time'  # Get newest first
            ).execute()
            
            for item in response.get('items', []):
                comment_data = item['snippet']['topLevelComment']['snippet']
                comment_date = comment_data['publishedAt']
                
                # Only include comments after our cutoff date
                if comment_date > after_date:
                    # Create a unique ID for this comment
                    comment_id = item['id']
                    
                    # Skip if we've already processed this comment
                    if store.get(f"comment_{comment_id}", default=None):
                        continue
                    
                    comment = {
                        'comment_id': comment_id,
                        'video_id': video_id,
                        'video_title': video_title,
                        'author': comment_data['authorDisplayName'],
                        'author_channel_id': comment_data.get('authorChannelId', {}).get('value', ''),
                        'text': comment_data['textDisplay'],
                        'published_at': comment_date,
                        'like_count': comment_data['likeCount'],
                        'reply_count': item['snippet']['totalReplyCount']
                    }
                    comments.append(comment)
                else:
                    # Comments are ordered by time, so we can stop if we hit older ones
                    # Note: This optimization may not always work due to API ordering
                    pass
            
            next_page_token = response.get('nextPageToken')
            if not next_page_token:
                break
                
    except Exception as e:
        # Some videos may have comments disabled
        if "commentsDisabled" in str(e):
            print(f"   ⚠️ Comments disabled for: {video_title[:40]}...")
        else:
            print(f"   ❌ Error fetching comments for {video_id}: {str(e)}")
    
    return comments

# ==============================================================================
# 🤖 AI CLASSIFICATION FUNCTION
# ==============================================================================

def classify_comment(comment_text, video_title):
    """
    Use OpenAI to classify a comment into one or more categories.
    Returns a dictionary with tags and sentiment score.
    """
    
    # Build the tag descriptions for the prompt
    tag_descriptions = "\n".join([f"- {tag}: {desc}" for tag, desc in COMMENT_TAGS.items()])
    
    # Create a detailed prompt for accurate classification
    prompt = f"""Analyze this YouTube comment and classify it.

VIDEO TITLE: {video_title}

COMMENT: {comment_text}

AVAILABLE TAGS (select ALL that apply):
{tag_descriptions}

INSTRUCTIONS:
1. Select one or more tags that best describe this comment
2. Provide a sentiment score from -1.0 (very negative) to 1.0 (very positive)
3. Mark as "urgent" if the person seems frustrated, angry, or needs immediate help

Respond in this exact JSON format:
{{
    "tags": ["tag1", "tag2"],
    "sentiment_score": 0.5,
    "reasoning": "Brief explanation of why these tags were chosen"
}}

Only respond with valid JSON, no other text."""

    try:
        # Call OpenAI API
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # Cost-effective and good at classification
            messages=[
                {
                    "role": "system",
                    "content": "You are a YouTube comment analyzer. You classify comments accurately and identify urgent issues. Always respond with valid JSON only."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.3,  # Lower temperature for more consistent results
            max_tokens=200
        )
        
        # Parse the response
        result_text = response.choices[0].message.content.strip()
        
        # Clean up the response (remove markdown code blocks if present)
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
        result_text = result_text.strip()
        
        result = json.loads(result_text)
        
        # Validate tags are from our list
        valid_tags = [tag for tag in result.get('tags', []) if tag in COMMENT_TAGS]
        
        return {
            'tags': valid_tags if valid_tags else ['positive'],  # Default to positive if no valid tags
            'sentiment_score': float(result.get('sentiment_score', 0)),
            'reasoning': result.get('reasoning', '')
        }
        
    except json.JSONDecodeError as e:
        print(f"   ⚠️ JSON parse error: {e}")
        return {'tags': ['positive'], 'sentiment_score': 0, 'reasoning': 'Classification failed'}
    except Exception as e:
        print(f"   ⚠️ Classification error: {e}")
        return {'tags': ['positive'], 'sentiment_score': 0, 'reasoning': 'Classification failed'}

# ==============================================================================
# 📧 EMAIL FUNCTIONS
# ==============================================================================

def send_urgent_alert(comment):
    """
    Send an email alert for urgent comments that need immediate attention.
    Uses a red/alarm theme to grab attention.
    """
    print(f"   📧 Sending urgent alert for comment by {comment['author']}...")
    
    # Create a nicely formatted HTML email with red/alarm theme
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #dc2626;">🚨 Urgent YouTube Comment Alert</h2>
        
        <div style="background-color: #fef2f2; border-left: 4px solid #dc2626; padding: 15px; margin: 20px 0;">
            <p style="margin: 0; color: #991b1b;"><strong>This comment needs your immediate attention!</strong></p>
        </div>
        
        <table style="width: 100%; border-collapse: collapse;">
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Video:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['video_title']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Author:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['author']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Posted:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['published_at']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Tags:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{', '.join(comment['tags'])}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Sentiment:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['sentiment_score']}</td>
            </tr>
        </table>
        
        <div style="background-color: #f3f4f6; padding: 15px; margin: 20px 0; border-radius: 8px;">
            <p style="margin: 0 0 10px 0;"><strong>Comment:</strong></p>
            <p style="margin: 0; color: #374151;">{comment['text']}</p>
        </div>
        
        <div style="margin-top: 20px;">
            <a href="https://www.youtube.com/watch?v={comment['video_id']}" 
               style="background-color: #dc2626; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">
                View on YouTube →
            </a>
        </div>
        
        <p style="color: #6b7280; font-size: 12px; margin-top: 30px;">
            AI Analysis: {comment.get('reasoning', 'N/A')}
        </p>
    </div>
    """
    
    try:
        email_response = resend.Emails.send({
            "from": SENDER_EMAIL,
            "to": [ALERT_EMAIL],
            "subject": f"🚨 Urgent Comment on: {comment['video_title'][:50]}",
            "html": html_content
        })
        print(f"   ✅ Urgent alert sent! Email ID: {email_response['id']}")
        return True
    except Exception as e:
        print(f"   ❌ Failed to send urgent alert: {e}")
        return False


def send_testimonial_alert(comment):
    """
    Send a celebratory email alert for testimonial-worthy comments.
    Uses a green/gold theme to highlight positive feedback.
    These are comments worth featuring on your website, social media, etc.
    """
    print(f"   📧 Sending testimonial alert for comment by {comment['author']}...")
    
    # Create a nicely formatted HTML email with green/celebratory theme
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #16a34a;">⭐ New Testimonial-Worthy Comment!</h2>
        
        <div style="background-color: #f0fdf4; border-left: 4px solid #16a34a; padding: 15px; margin: 20px 0;">
            <p style="margin: 0; color: #166534;"><strong>Someone left an amazing comment worth featuring! 🎉</strong></p>
        </div>
        
        <table style="width: 100%; border-collapse: collapse;">
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Video:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['video_title']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Author:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['author']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Posted:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{comment['published_at']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Likes:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">👍 {comment['like_count']}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;"><strong>Sentiment:</strong></td>
                <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">{"🟢" if comment['sentiment_score'] > 0.5 else "🟡"} {comment['sentiment_score']}</td>
            </tr>
        </table>
        
        <div style="background-color: #f0fdf4; padding: 20px; margin: 20px 0; border-radius: 8px; border: 1px solid #bbf7d0;">
            <p style="margin: 0 0 10px 0; color: #166534;"><strong>💬 The Comment:</strong></p>
            <p style="margin: 0; color: #374151; font-size: 16px; line-height: 1.6; font-style: italic;">"{comment['text']}"</p>
            <p style="margin: 10px 0 0 0; color: #6b7280; font-size: 13px;">— {comment['author']}</p>
        </div>
        
        <div style="margin-top: 20px; display: flex; gap: 10px;">
            <a href="https://www.youtube.com/watch?v={comment['video_id']}" 
               style="background-color: #16a34a; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">
                View on YouTube →
            </a>
        </div>
        
        <p style="color: #6b7280; font-size: 12px; margin-top: 30px;">
            AI Analysis: {comment.get('reasoning', 'N/A')}
        </p>
    </div>
    """
    
    try:
        email_response = resend.Emails.send({
            "from": SENDER_EMAIL,
            "to": [ALERT_EMAIL],
            "subject": f"⭐ New Testimonial on: {comment['video_title'][:50]}",
            "html": html_content
        })
        print(f"   ✅ Testimonial alert sent! Email ID: {email_response['id']}")
        return True
    except Exception as e:
        print(f"   ❌ Failed to send testimonial alert: {e}")
        return False


def send_daily_summary_email(stats, tag_counts, urgent_comments, testimonial_comments):
    """
    Send a daily summary email with an overview of all analyzed comments.
    Only called when there are new comments to report.
    Uses a blue/neutral professional theme.
    """
    print("\n📧 Sending daily summary email...")
    
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # -------------------------------------------------------------------
    # Build the tag breakdown rows (only show tags that have counts > 0)
    # -------------------------------------------------------------------
    tag_rows = ""
    # Sort by count descending so the most common tags appear first
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    for tag, count in sorted_tags:
        if count > 0:
            # Pick a color for each tag to make the table visually scannable
            tag_colors = {
                "urgent": "#dc2626",
                "testimonial": "#16a34a",
                "negative": "#ea580c",
                "question": "#2563eb",
                "content_idea": "#7c3aed",
                "brand_mention": "#0891b2",
                "collaboration": "#c026d3",
                "spam": "#6b7280",
                "positive": "#16a34a",
                "feature_request": "#7c3aed"
            }
            color = tag_colors.get(tag, "#374151")
            tag_rows += f"""
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;">
                    <span style="background-color: {color}; color: white; padding: 2px 8px; border-radius: 12px; font-size: 12px;">{tag}</span>
                </td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: center; font-weight: bold;">{count}</td>
            </tr>
            """
    
    # -------------------------------------------------------------------
    # Build the urgent comments section (if any)
    # -------------------------------------------------------------------
    urgent_section = ""
    if urgent_comments:
        urgent_items = ""
        for c in urgent_comments:
            urgent_items += f"""
            <div style="background-color: #fef2f2; padding: 12px; margin: 8px 0; border-radius: 6px; border-left: 3px solid #dc2626;">
                <p style="margin: 0 0 4px 0; font-size: 13px; color: #991b1b;"><strong>{c['author']}</strong> on <em>{c['video_title'][:50]}</em></p>
                <p style="margin: 0; color: #374151; font-size: 13px;">{c['text'][:150]}{'...' if len(c['text']) > 150 else ''}</p>
            </div>
            """
        urgent_section = f"""
        <div style="margin: 20px 0;">
            <h3 style="color: #dc2626; margin-bottom: 10px;">🚨 Urgent Comments ({len(urgent_comments)})</h3>
            {urgent_items}
        </div>
        """
    
    # -------------------------------------------------------------------
    # Build the testimonials section (if any)
    # -------------------------------------------------------------------
    testimonial_section = ""
    if testimonial_comments:
        testimonial_items = ""
        for c in testimonial_comments:
            testimonial_items += f"""
            <div style="background-color: #f0fdf4; padding: 12px; margin: 8px 0; border-radius: 6px; border-left: 3px solid #16a34a;">
                <p style="margin: 0 0 4px 0; font-size: 13px; color: #166534;"><strong>{c['author']}</strong> on <em>{c['video_title'][:50]}</em></p>
                <p style="margin: 0; color: #374151; font-size: 13px; font-style: italic;">"{c['text'][:150]}{'...' if len(c['text']) > 150 else ''}"</p>
            </div>
            """
        testimonial_section = f"""
        <div style="margin: 20px 0;">
            <h3 style="color: #16a34a; margin-bottom: 10px;">⭐ Testimonials ({len(testimonial_comments)})</h3>
            {testimonial_items}
        </div>
        """
    
    # -------------------------------------------------------------------
    # Assemble the full summary email
    # -------------------------------------------------------------------
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #1e40af;">📊 YouTube Comments Daily Summary</h2>
        <p style="color: #6b7280; font-size: 14px;">{today}</p>
        
        <!-- Quick Stats Banner -->
        <div style="display: flex; gap: 0; margin: 20px 0;">
            <div style="flex: 1; background-color: #eff6ff; padding: 15px; text-align: center; border-radius: 8px 0 0 8px; border: 1px solid #bfdbfe;">
                <p style="margin: 0; font-size: 28px; font-weight: bold; color: #1e40af;">{stats['comments_analyzed']}</p>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #6b7280;">Comments Analyzed</p>
            </div>
            <div style="flex: 1; background-color: #fef2f2; padding: 15px; text-align: center; border: 1px solid #fecaca;">
                <p style="margin: 0; font-size: 28px; font-weight: bold; color: #dc2626;">{stats['urgent_alerts_sent']}</p>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #6b7280;">Urgent</p>
            </div>
            <div style="flex: 1; background-color: #f0fdf4; padding: 15px; text-align: center; border-radius: 0 8px 8px 0; border: 1px solid #bbf7d0;">
                <p style="margin: 0; font-size: 28px; font-weight: bold; color: #16a34a;">{stats['testimonial_alerts_sent']}</p>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #6b7280;">Testimonials</p>
            </div>
        </div>
        
        <!-- Run Overview -->
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">Videos Scanned</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right; font-weight: bold;">{stats['videos_processed']}</td>
            </tr>
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">New Comments Found</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right; font-weight: bold;">{stats['comments_found']}</td>
            </tr>
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">Comments Analyzed</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right; font-weight: bold;">{stats['comments_analyzed']}</td>
            </tr>
        </table>
        
        <!-- Tag Breakdown -->
        <h3 style="color: #374151; margin-top: 25px;">🏷️ Tag Breakdown</h3>
        <table style="width: 100%; border-collapse: collapse;">
            <tr style="background-color: #f9fafb;">
                <th style="padding: 8px 12px; text-align: left; border-bottom: 2px solid #e5e7eb; color: #6b7280; font-size: 12px;">TAG</th>
                <th style="padding: 8px 12px; text-align: center; border-bottom: 2px solid #e5e7eb; color: #6b7280; font-size: 12px;">COUNT</th>
            </tr>
            {tag_rows}
        </table>
        
        {urgent_section}
        {testimonial_section}
        
        <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #e5e7eb;">
            <p style="color: #9ca3af; font-size: 11px; text-align: center;">
                Generated by YouTube Comment Analyzer • PyRunner Automation
            </p>
        </div>
    </div>
    """
    
    try:
        email_response = resend.Emails.send({
            "from": SENDER_EMAIL,
            "to": [ALERT_EMAIL],
            "subject": f"📊 YouTube Comments Summary — {stats['comments_analyzed']} comments analyzed ({today})",
            "html": html_content
        })
        print(f"✅ Summary email sent! Email ID: {email_response['id']}")
        return True
    except Exception as e:
        print(f"❌ Failed to send summary email: {e}")
        return False

# ==============================================================================
# 💾 SAVE COMMENT TO DATASTORE
# ==============================================================================

def save_comment(comment):
    """
    Save an analyzed comment to the DataStore.
    """
    key = f"comment_{comment['comment_id']}"
    store[key] = comment
    
    # Also update the summary counts
    update_summary_counts(comment['tags'])


def update_summary_counts(tags):
    """
    Update the running count of each tag type for reporting.
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    summary_key = f"summary_{today}"
    
    # Get existing summary or create new one
    summary = store.get(summary_key, default={
        'date': today,
        'total_comments': 0,
        'tag_counts': {tag: 0 for tag in COMMENT_TAGS.keys()}
    })
    
    # Update counts
    summary['total_comments'] += 1
    for tag in tags:
        if tag in summary['tag_counts']:
            summary['tag_counts'][tag] += 1
    
    # Save updated summary
    store[summary_key] = summary

# ==============================================================================
# 📊 DAILY SUMMARY REPORT (Terminal Output)
# ==============================================================================

def print_daily_summary():
    """
    Print a summary of today's analyzed comments to the terminal.
    """
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    summary_key = f"summary_{today}"
    summary = store.get(summary_key, default=None)
    
    print("\n" + "=" * 60)
    print("📊 DAILY SUMMARY REPORT")
    print("=" * 60)
    
    if not summary:
        print("No comments analyzed today.")
        return
    
    print(f"📅 Date: {summary['date']}")
    print(f"💬 Total Comments Analyzed: {summary['total_comments']}")
    print("\n📈 Comments by Tag:")
    print("-" * 40)
    
    # Sort tags by count (highest first)
    sorted_tags = sorted(
        summary['tag_counts'].items(),
        key=lambda x: x[1],
        reverse=True
    )
    
    for tag, count in sorted_tags:
        if count > 0:
            # Create a simple bar chart
            bar = "█" * min(count, 20)
            print(f"   {tag:20} {count:4} {bar}")
    
    print("=" * 60)

# ==============================================================================
# 🚀 MAIN EXECUTION
# ==============================================================================

def main():
    """
    Main function that orchestrates the entire comment analysis process.
    """
    # Track statistics for this run
    stats = {
        'videos_processed': 0,
        'comments_found': 0,
        'comments_analyzed': 0,
        'urgent_alerts_sent': 0,
        'testimonial_alerts_sent': 0,
        'errors': 0
    }
    
    # Collect urgent and testimonial comments for the summary email
    urgent_comments = []
    testimonial_comments = []
    
    # Get the cutoff date for comment analysis
    after_date = get_last_run_date()
    
    # Step 1: Fetch all videos from the channel
    videos = get_channel_videos()
    
    if not videos:
        print("❌ No videos found. Check your CHANNEL_ID.")
        return
    
    # Step 2: Process each video
    print(f"\n🔍 Processing comments from {len(videos)} videos...")
    print("-" * 60)
    
    all_new_comments = []
    
    for i, video in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}] 📹 {video['title'][:50]}...")
        
        # Fetch new comments for this video
        comments = get_video_comments(
            video['video_id'],
            video['title'],
            after_date
        )
        
        if comments:
            print(f"   Found {len(comments)} new comment(s)")
            all_new_comments.extend(comments)
            stats['comments_found'] += len(comments)
        
        stats['videos_processed'] += 1
    
    print(f"\n{'=' * 60}")
    print(f"📊 Found {len(all_new_comments)} total new comments to analyze")
    print("=" * 60)
    
    # Step 3: Analyze each comment with AI
    if all_new_comments:
        print("\n🤖 Analyzing comments with AI...")
        print("-" * 60)
        
        for i, comment in enumerate(all_new_comments, 1):
            print(f"\n[{i}/{len(all_new_comments)}] Analyzing comment by {comment['author']}...")
            print(f"   Preview: {comment['text'][:80]}...")
            
            # Classify the comment
            classification = classify_comment(comment['text'], comment['video_title'])
            
            # Add classification results to comment
            comment['tags'] = classification['tags']
            comment['sentiment_score'] = classification['sentiment_score']
            comment['reasoning'] = classification['reasoning']
            comment['analyzed_at'] = datetime.now(timezone.utc).isoformat()
            
            print(f"   🏷️ Tags: {', '.join(comment['tags'])}")
            print(f"   📈 Sentiment: {comment['sentiment_score']}")
            
            # Save to DataStore
            save_comment(comment)
            stats['comments_analyzed'] += 1
            
            # Check if urgent — send dedicated alert email
            if 'urgent' in comment['tags']:
                print(f"   🚨 URGENT comment detected!")
                urgent_comments.append(comment)
                if send_urgent_alert(comment):
                    stats['urgent_alerts_sent'] += 1
            
            # Check if testimonial — send dedicated celebratory email
            if 'testimonial' in comment['tags']:
                print(f"   ⭐ TESTIMONIAL comment detected!")
                testimonial_comments.append(comment)
                if send_testimonial_alert(comment):
                    stats['testimonial_alerts_sent'] += 1
    
    # Step 4: Save the run timestamp for next time
    save_run_timestamp()
    
    # Step 5: Print terminal summary
    print_daily_summary()
    
    # Step 6: Send summary email (only if we had new comments)
    if stats['comments_analyzed'] > 0:
        # Get today's tag counts from DataStore for the email
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        summary_data = store.get(f"summary_{today}", default={})
        tag_counts = summary_data.get('tag_counts', {})
        
        send_daily_summary_email(stats, tag_counts, urgent_comments, testimonial_comments)
    else:
        print("\n📭 No new comments found — skipping summary email.")
    
    # Final stats
    print("\n" + "=" * 60)
    print("✅ ANALYSIS COMPLETE!")
    print("=" * 60)
    print(f"   📹 Videos processed:        {stats['videos_processed']}")
    print(f"   💬 New comments found:      {stats['comments_found']}")
    print(f"   🤖 Comments analyzed:       {stats['comments_analyzed']}")
    print(f"   🚨 Urgent alerts sent:      {stats['urgent_alerts_sent']}")
    print(f"   ⭐ Testimonial alerts sent:  {stats['testimonial_alerts_sent']}")
    print("=" * 60)


# Run the main function
if __name__ == "__main__":
    main()