import datetime
import simplejson
import re

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db.models import get_model, Q
from django.http import HttpResponse, HttpResponseRedirect, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404

from djangohelpers.lib import rendered_with, allow_http

from clumper import Clumper

from assetmgr.lib import annotated_by, get_active_filters
from assetmgr.views import homepage_asset_json

from courseaffils.lib import get_public_name, in_course_or_404, users_in_course
from courseaffils.models import Course

from djangosherd.models import DiscussionIndex

from discussions.utils import get_discussions, get_course_discussions

from mediathread_main.models import UserSetting
from mediathread_main import course_details

from projects.lib import homepage_project_json

from reports.views import is_unanswered_assignment

from tagging.models import Tag


ThreadedComment = get_model('threadedcomments', 'threadedcomment')
Collaboration = get_model('structuredcollaboration', 'collaboration')
CollaborationPolicyRecord = get_model('structuredcollaboration', 'collaborationpolicyrecord')
Asset = get_model('assetmgr', 'asset')
SherdNote = get_model('djangosherd', 'sherdnote')
Project = get_model('projects', 'project')
ProjectVersion = get_model('projects', 'projectversion')
User = get_model('auth', 'user')
#for portal
Comment = get_model('comments', 'comment')
ContentType = get_model('contenttypes', 'contenttype')
SupportedSource = get_model('assetmgr', 'supportedsource')
        
#returns important setting information for all web pages.
def django_settings(request):
    whitelist = ['PUBLIC_CONTACT_EMAIL',
                 'CONTACT_US_DESTINATION',
                 'FLOWPLAYER_SWF_LOCATION',
                 'DEBUG',
                 'REVISION'
                 ]

    rv = {'settings':dict([(k, getattr(settings, k, None)) for k in whitelist]),
          'EXPERIMENTAL':request.COOKIES.has_key('experimental'),
          }
    if request.course:
        rv['is_course_faculty'] = request.course.is_faculty(request.user)

    return rv


@rendered_with('dashboard/notifications.html')
@allow_http("GET")
def notifications(request):
    c = request.course

    if not c:
        return HttpResponseRedirect('/accounts/login/')

    user = request.user
    if user.is_staff and request.GET.has_key('as'):
        user = get_object_or_404(User, username=request.GET['as'])

    class_feed = []

    #personal feed
    my_assets = {}
    for n in SherdNote.objects.filter(author=user, asset__course=c):
        my_assets[str(n.asset_id)] = 1
    for comment in Comment.objects.filter(user=user):
        if c == getattr(comment.content_object, 'course', None):
            my_assets[str(comment.object_pk)] = 1
    my_discussions = [d.collaboration_id for d in DiscussionIndex.objects
                      .filter(participant=user,
                              collaboration__context=request.collaboration_context
                              )]

    my_feed = Clumper(Comment.objects
                    .filter(content_type=ContentType.objects.get_for_model(Asset),
                            object_pk__in=my_assets.keys())
                    .order_by('-submit_date'), #so the newest ones show up
                    SherdNote.objects.filter(asset__in=my_assets.keys(),
                                             #no global annotations
                                             #warning: if we include global annotations
                                             #we need to stop it from autocreating one on-view
                                             #of the asset somehow
                                             range1__isnull=False
                                             )
                    .order_by('-added'),
                    Project.objects
                    .filter(Q(participants=user.pk) | Q(author=user.pk), course=c)
                    .order_by('-modified'),
                    DiscussionIndex.with_permission(request,
                                                    DiscussionIndex.objects
                                                    .filter(Q(Q(asset__in=my_assets.keys())
                                                              | Q(collaboration__in=my_discussions)
                                                              | Q(collaboration__user=request.user)
                                                              | Q(collaboration__group__user=request.user),
                                                              participant__isnull=False
                                                              )
                                                       )
                                                       .order_by('-modified')
                                                    ),
                    )

    return {
        'my_feed':my_feed,
        'space_viewer': user,
        "help_notifications": UserSetting.get_setting(user, "help_notifications", True)
    }

def date_filter_for(attr):

    def date_filter(asset, date_range, user):
        """
        we want the added/modified date *for the user*, ie when the
        user first/last edited/created an annotation on the asset --
        not when the asset itself was created/modified.

        this is really really ugly.  wouldn't be bad in sql but i don't
        trust my sql well enough. after i write some tests maybe?
        """
        if attr == "added":
            annotations = SherdNote.objects.filter(asset=asset, author=user)
            annotations = annotations.order_by('added')
            # get the date on which the earliest annotation was created
            date = annotations[0].added

        elif attr == "modified":
            annotations = SherdNote.objects.filter(asset=asset, author=user)
            # get the date on which the most recent annotation was created
            annotations = annotations.order_by('-added')
            added_date = annotations[0].added
            # also get the most recent modification date of any annotation
            annotations = annotations.order_by('-modified')
            modified_date = annotations[0].modified

            if added_date > modified_date:
                date = added_date
            else:
                date = modified_date

        date = datetime.date(date.year, date.month, date.day)

        today = datetime.date.today()

        if date_range == 'today':
            return date == today
        if date_range == 'yesterday':
            before_yesterday = today + datetime.timedelta(-2)
            return date > before_yesterday and date < today
        if date_range == 'lastweek':
            over_a_week_ago = today + datetime.timedelta(-8)
            return date > over_a_week_ago
    return date_filter

filter_by = {
    'tag': lambda asset, tag, user: filter(lambda x: x.name == tag,
                                           asset.tags()),
    'added': date_filter_for('added'),
    'modified': date_filter_for('modified')
}

def get_prof_feed(course, request):
    prof_feed = { 'assets': [], #assets.filter(c.faculty_filter).order_by('-added'),
                  'projects': [], # we'll add these directly below, to ensure security filters
                  'assignments': [],
                  'tags': Tag.objects.get_for_object(course)
                 }
    prof_projects = Project.objects.filter(
        course.faculty_filter).order_by('title')
    for project in prof_projects:
        if project.class_visible():
            if project.is_assignment(request):
                prof_feed['assignments'].append(project)
            else:
                prof_feed['projects'].append(project)

    prof_feed['show'] = (prof_feed['assets'] or prof_feed['projects'] or prof_feed['assignments'] or prof_feed['tags'])
    return prof_feed

@rendered_with('homepage.html')
def triple_homepage(request):
    if not request.course:
        return HttpResponseRedirect('/accounts/login/')
    
    logged_in_user = request.user
    classwork_owner = request.user # Viewing your own work by default
    if request.GET.has_key('username'):
        user_name = request.GET['username']
        in_course_or_404(user_name, request.course)
        classwork_owner = get_object_or_404(User, username=user_name)

    context = {
        'classwork_owner': classwork_owner,
        'help_homepage_instructor_column': UserSetting.get_setting(logged_in_user, "help_homepage_instructor_column", True),
        'help_homepage_classwork_column':  UserSetting.get_setting(logged_in_user, "help_homepage_classwork_column", True),

        'faculty_feed': get_prof_feed(request.course, request),
        'is_faculty': request.course.is_faculty(logged_in_user),
        'discussions': get_course_discussions(request.course),
        
        'msg': request.GET.get('msg', ''),
        'tag': request.GET.get('tag', ''),
        'view': request.GET.get('view', '')
    }
    return context
    

@allow_http("GET")
def your_records(request, record_owner_name):
    """
    An ajax-only request to retrieve a specified user's projects, assignment responses and selections
        
    """    
    if not request.is_ajax():
        raise Http404()

    course = request.course    
    in_course_or_404(record_owner_name, course)
    record_owner = get_object_or_404(User, username = record_owner_name)
    logged_in_user = request.user

    assets = annotated_by(Asset.objects.filter(course = course),
                          record_owner,
                          include_archives = False)
            
    projects = Project.get_user_projects(record_owner, course).order_by('-modified')
    if not record_owner == logged_in_user:
        projects = [p for p in projects if p.visible(request)]

    project_type = ContentType.objects.get_for_model(Project)
    assignments = []
    for assignment in Project.objects.filter(course.faculty_filter):
        if not assignment.visible(request):
            continue
        if assignment in projects:
            continue
        if is_unanswered_assignment(assignment, record_owner, request, project_type):
            assignments.append(assignment)
            
    return get_records(request, record_owner, projects, assignments, assets)

@allow_http("GET")
def all_records(request):
    """
    An ajax-only request to retrieve a course's projects, assignment responses and selections
        
    """    

    if not request.is_ajax():
        raise Http404()

    if not request.user.is_staff:
        in_course_or_404(request.user.username, request.course)
        
    course = request.course
    archives = list(request.course.asset_set.archives())
    assets = [a for a in Asset.objects.filter(course=course).extra(
        select={'lower_title': 'lower(assetmgr_asset.title)'}
        ).select_related().order_by('lower_title')
          if a not in archives]
    
    projects = [p for p in Project.objects.filter(course=course,
                                                  submitted=True).order_by('title')
                                                  if p.visible(request)]

    return get_records(request, None, projects, [], assets);

    
def get_records(request, record_owner, projects, assignments, assets):
    course = request.course
    logged_in_user = request.user
    
    # Can the record_owner edit the records
    viewing_my_records = (record_owner == logged_in_user)
    viewing_faculty_records = record_owner and course.is_faculty(record_owner)

    # Allow the logged in user to add assets to his composition 
    citable = request.GET.has_key('citable') and request.GET.get('citable') == 'true'
    
    # Is the current user faculty OR staff
    is_faculty = course.is_faculty(logged_in_user)
    
    # Does the course allow viewing other user selections?
    owner_selections_are_visible = course_details.all_selections_are_visible(course) or \
        viewing_my_records or viewing_faculty_records or is_faculty
        
    # Filter the assets 
    for fil in filter_by:
        filter_value = request.GET.get(fil)
        if filter_value:
            assets = [asset for asset in assets
                      if filter_by[fil](asset, filter_value, record_owner)]
    
    active_filters = get_active_filters(request, filter_by)
    
    # Spew out json for the assets 
    asset_json = []
    options = {
        'owner_selections_are_visible': request.GET.has_key('annotations') and owner_selections_are_visible,
        'all_selections_are_visible': course_details.all_selections_are_visible(course) or is_faculty,
        'can_edit': viewing_my_records,
        'citable': citable
    }
            
    for asset in assets:
        asset_json.append(homepage_asset_json(request, asset, logged_in_user, record_owner, options))
        
    # Spew out json for the projects
    project_json = []
    for p in projects:
        project_json.append(homepage_project_json(request, p, viewing_my_records))
        
    # Tags
    tags = []
    if record_owner:
        if owner_selections_are_visible:
            # Tags for selected user
            tags = Tag.objects.usage_for_queryset(
                record_owner.sherdnote_set.filter(asset__course=course),
                counts=True)
    else:
        if owner_selections_are_visible:
            # Tags for the whole class
            tags = Tag.objects.usage_for_queryset(
                SherdNote.objects.filter(asset__course=course),
                counts=True)
        else:
            # Tags for myself and faculty members
            tags = Tag.objects.usage_for_queryset(
                       logged_in_user.sherdnote_set.filter(asset__course=course),
                       counts=True)
            
            for f in course.faculty:
                tags.extend(Tag.objects.usage_for_queryset(
                                f.sherdnote_set.filter(asset__course=course),
                                counts=True))
    

    tags.sort(lambda a, b:cmp(a.name.lower(), b.name.lower()))

    # Assemble the context
    data = { 'assets': asset_json,
             'assignments' : [ {'id': a.id, 'url': a.get_absolute_url(), 'title': a.title, 'modified': a.modified.strftime("%m/%d/%y %I:%M %p")} for a in assignments],
             'projects' : project_json,
             'tags': [ { 'name': tag.name } for tag in tags ],
             'active_filters': active_filters,
             'space_viewer'  : { 'username': logged_in_user.username, 'public_name': get_public_name(logged_in_user, request), 'can_manage': (logged_in_user.is_staff and not record_owner) },
             'editable' : viewing_my_records,
             'citable' : citable,
             'owners' : [{ 'username': m.username, 'public_name': get_public_name(m, request) } for m in request.course.members],
             'compositions' : len(projects) > 0 or len(assignments) > 0,
             'is_faculty': is_faculty,
            }
    
    if record_owner:
        data['space_owner'] = { 'username': record_owner.username, 'public_name': get_public_name(record_owner, request) }

    json_stream = simplejson.dumps(data, indent=2)
    return HttpResponse(json_stream, mimetype='application/json')

@allow_http("GET", "POST")
@rendered_with('dashboard/dashboard_home.html')
def dashboard(request):
    user = request.user
    if not request.course.is_faculty(user):
        return HttpResponseForbidden("forbidden")
    
    return { 
       "space_viewer": user,
       "help_dashboard_nav_actions": UserSetting.get_setting(user, "help_dashboard_nav_actions", False),
       "help_dashboard_nav_reports": UserSetting.get_setting(user, "help_dashboard_nav_reports", False)      
    }

@allow_http("GET", "POST")
@rendered_with('dashboard/class_addsource.html')
def class_addsource(request):
    key = course_details.UPLOAD_PERMISSION_KEY
    
    c = request.course
    user = request.user
    if not request.course.is_faculty(user):
        return HttpResponseForbidden("forbidden")
    
    upload_enabled = False
    for a in c.asset_set.archives().order_by('title'):
        attribute = a.metadata().get('upload', 0)
        value = attribute[0] if hasattr(attribute, 'append') else attribute
        if value and int(value) == 1:
            upload_enabled = True
            break
        
    context = {
            'asset_request': request.GET,
            'course': c,
            'supported_archives': SupportedSource.objects.all().order_by("title"), # sort by title
            'space_viewer': request.user,
            'is_staff': request.user.is_staff,
            'newsrc' : request.GET.get('newsrc', ''),
            'upload_enabled': upload_enabled,
            'permission_levels': course_details.UPLOAD_PERMISSION_LEVELS,
            'help_video_upload': UserSetting.get_setting(user, "help_video_upload", True),
            'help_supported_collections': UserSetting.get_setting(user, "help_supported_collections", True),
            'help_dashboard_nav_actions': UserSetting.get_setting(user, "help_dashboard_nav_actions", False),
            'help_dashboard_nav_reports': UserSetting.get_setting(user, "help_dashboard_nav_reports", False)
        }

    if request.method == "GET":
        context[key] = int(c.get_detail(key, course_details.UPLOAD_PERMISSION_DEFAULT))    
    else:
        upload_permission = request.POST.get(key)
        request.course.add_detail(key, upload_permission)
        context['changes_saved'] = True
        context[key] = int(upload_permission)
            
    return context

@allow_http("GET", "POST")
@rendered_with('dashboard/class_settings.html')
def class_settings(request):
    c = request.course
    user = request.user
    if not request.course.is_faculty(user):
        return HttpResponseForbidden("forbidden")
    
    context = {
            'asset_request': request.GET,
            'course': c,
            'space_viewer': request.user,
            'is_staff': request.user.is_staff,
            'help_public_compositions': UserSetting.get_setting(user, "help_public_compositions", True),
            'help_selection_visibility': UserSetting.get_setting(user, "help_selection_visibility", True),
    }
    
    public_composition_key = course_details.ALLOW_PUBLIC_COMPOSITIONS_KEY
    context[course_details.ALLOW_PUBLIC_COMPOSITIONS_KEY] = int(c.get_detail(public_composition_key, course_details.ALLOW_PUBLIC_COMPOSITIONS_DEFAULT))
    
    selection_visibility_key = course_details.SELECTION_VISIBILITY_KEY
    context[course_details.SELECTION_VISIBILITY_KEY] = int(c.get_detail(selection_visibility_key, course_details.SELECTION_VISIBILITY_DEFAULT))
    
    if request.method == "POST":
        if request.POST.has_key(selection_visibility_key):
            selection_visibility_value = int(request.POST.get(selection_visibility_key))
            request.course.add_detail(selection_visibility_key, selection_visibility_value)
            context[selection_visibility_key] = selection_visibility_value

        if request.POST.has_key(public_composition_key):
            public_composition_value = int(request.POST.get(public_composition_key))
            request.course.add_detail(public_composition_key, public_composition_value)
            context[public_composition_key] = public_composition_value
            
            if public_composition_value == 0:
                # Check any existing projects -- if they are world publishable, turn this feature OFF
                projects = Project.objects.filter(course=c)
                for p in projects:
                    try:
                        col = Collaboration.get_associated_collab(p)
                        if col._policy.policy_name == 'PublicEditorsAreOwners':
                            col.policy = 'CourseProtected'
                            col.save()
                    except:
                        pass
                
        context['changes_saved'] = True
                
    return context

@allow_http("POST")
def set_user_setting(request, user_name):
    if not request.is_ajax():
        raise Http404()
    
    user = get_object_or_404(User, username=user_name)
    name = request.POST.get("name")
    value = request.POST.get("value")
    
    UserSetting.set_setting(user, name, value)
    
    json_stream = simplejson.dumps({ 'success': True })
    return HttpResponse(json_stream, mimetype='application/json')
