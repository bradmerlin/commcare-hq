# coding=utf-8
from collections import defaultdict
from datetime import datetime
import re
from corehq.apps.app_manager.const import APP_V1, APP_V2
from couchdbkit.exceptions import BadValueError
from couchdbkit.ext.django.schema import *
from django.conf import settings
from django.contrib.auth.models import get_hexdigest
from django.core.urlresolvers import reverse
from django.http import Http404
from restkit.errors import ResourceError
import commcare_translations
from corehq.apps.app_manager import fixtures
from corehq.apps.app_manager.xform import XForm, parse_xml as _parse_xml, namespaces as NS, XFormError, XFormValidationError
from corehq.apps.builds.models import CommCareBuild, BuildSpec, CommCareBuildConfig, BuildRecord
from corehq.apps.hqmedia.models import HQMediaMixin
from corehq.apps.translations.models import TranslationMixin
from corehq.apps.users.util import cc_user_domain
from corehq.util import bitly
import current_builds
from dimagi.utils.couch.undo import DeleteRecord, DELETED_SUFFIX
from dimagi.utils.web import get_url_base, parse_int
from copy import deepcopy
from corehq.apps.domain.models import Domain
import hashlib
from django.template.loader import render_to_string
from urllib2 import urlopen, URLError
from urlparse import urljoin
from corehq.apps.domain.decorators import login_and_domain_required
import langcodes
import util


import random
from dimagi.utils.couch.database import get_db
import json
from couchdbkit.resource import ResourceNotFound
import tempfile
import os
from utilities.profile import profile as profile_decorator, profile
import logging

MISSING_DEPENDECY = \
"""Aw shucks, someone forgot to install the google chart library
on this machine and this feature needs it. To get it, run
easy_install pygooglechart.  Until you do that this won't work.
"""

DETAIL_TYPES = ['case_short', 'case_long', 'ref_short', 'ref_long']

def _dsstr(self):
    return ", ".join(json.dumps(self.to_json()), self.schema)
#DocumentSchema.__repr__ = _dsstr


def _rename_key(dct, old, new):
    if old in dct:
        if new in dct and dct[new]:
            dct["%s_backup_%s" % (new, hex(random.getrandbits(32))[2:-1])] = dct[new]
        dct[new] = dct[old]
        del dct[old]

def load_case_reserved_words():
    with open(os.path.join(os.path.dirname(__file__), 'static', 'app_manager', 'json', 'case-reserved-words.json')) as f:
        return json.load(f)

def load_default_user_registration():
    with open(os.path.join(os.path.dirname(__file__), 'data', 'register_user.xhtml')) as f:
        return f.read()

def load_custom_commcare_properties():
    with open(os.path.join(os.path.dirname(__file__), 'static', 'app_manager', 'json', 'custom-commcare-properties.json')) as f:
        return json.load(f)

def authorize_xform_edit(view):
    def authorized_view(request, xform_id):
        @login_and_domain_required
        def wrapper(req, domain):
            pass
        _, app = Form.get_form(*xform_id.split('__'), and_app=True)
        if wrapper(request, app.domain):
            # If login_and_domain_required intercepted wrapper
            # and returned an HttpResponse of its own
            #return HttpResponseForbidden()
            return wrapper(request, app.domain)
        else:
            return view(request, xform_id)
    return authorized_view

def get_xform(form_unique_id):
    "For use with xep_hq_server's GET_XFORM hook."
    form = Form.get_form(form_unique_id)
    return form.source
def put_xform(form_unique_id, source):
    "For use with xep_hq_server's PUT_XFORM hook."
    form, app = Form.get_form(form_unique_id, and_app=True)
    form.source = source
    app.save()

class IndexedSchema(DocumentSchema):
    """
    Abstract class.
    Meant for documents that appear in a list within another document
    and need to know their own position within that list.

    """
    def with_id(self, i, parent):
        self._i = i
        self._parent = parent
        return self
    @property
    def id(self):
        return self._i
    def __eq__(self, other):
        return other and (self.id == other.id) and (self._parent == other._parent)

class FormActionCondition(DocumentSchema):
    """
    The condition under which to open/update/close a case/referral

    Either {'type': 'if', 'question': '/xpath/to/node', 'answer': 'value'}
    in which case the action takes place if question has answer answer,
    or {'type': 'always'} in which case the action always takes place.
    """
    type        = StringProperty(choices=["if", "always", "never"], default="never")
    question    = StringProperty()
    answer      = StringProperty()

class FormAction(DocumentSchema):
    """
    Corresponds to Case XML

    """
    condition   = SchemaProperty(FormActionCondition)
    def is_active(self):
        return self.condition.type in ('if', 'always')

class UpdateCaseAction(FormAction):
    update  = DictProperty()

class PreloadAction(FormAction):
    preload = DictProperty()
    def is_active(self):
        return bool(self.preload)

class UpdateReferralAction(FormAction):
    followup_date   = StringProperty()
    def get_followup_date(self):
        if self.followup_date:
            return "if(date({followup_date}) >= date(today()), {followup_date}, date(today() + 2))".format(
                followup_date = self.followup_date,
            )
        return self.followup_date or "date(today() + 2)"

class OpenReferralAction(UpdateReferralAction):
    name_path       = StringProperty()

class OpenCaseAction(FormAction):
    name_path   = StringProperty()
    external_id = StringProperty()


class FormActions(DocumentSchema):
    open_case       = SchemaProperty(OpenCaseAction)
    update_case     = SchemaProperty(UpdateCaseAction)
    close_case      = SchemaProperty(FormAction)
    open_referral   = SchemaProperty(OpenReferralAction)
    update_referral = SchemaProperty(UpdateReferralAction)
    close_referral  = SchemaProperty(FormAction)

    case_preload    = SchemaProperty(PreloadAction)
    referral_preload= SchemaProperty(PreloadAction)


class FormSource(object):
    def __get__(self, form, form_cls):
        unique_id = form.get_unique_id()
        source = form.dynamic_properties().get('contents')
        if source is None:
            app = form.get_app()
            filename = "%s.xml" % unique_id
            if app._attachments and filename in app._attachments and app._attachments[filename]["length"] > 0:
                source = form.get_app().fetch_attachment(filename)
            else:
                source = ''
        return source

    def __set__(self, form, value):
        unique_id = form.get_unique_id()
        form.contents = value
        app = form.get_app()
        def pre_save():
            if form.dynamic_properties().has_key('contents'):
                del form.contents
        def post_save():
            app.put_attachment(value, '%s.xml' % unique_id)
        form.validation_cache = None
        try:
            form.xmlns = form.wrapped_xform().data_node.tag_xmlns
        except Exception:
            form.xmlns = None
        app.register_pre_save(pre_save)
        app.register_post_save(post_save)


class FormBase(DocumentSchema):
    """
    Part of a Managed Application; configuration for a form.
    Translates to a second-level menu on the phone

    """

    name        = DictProperty()
    unique_id   = StringProperty()
    requires    = StringProperty(choices=["case", "referral", "none"], default="none")
    actions     = SchemaProperty(FormActions)
    show_count  = BooleanProperty(default=False)
    xmlns       = StringProperty()
#    contents    = StringProperty()
    source      = FormSource()
    validation_cache = StringProperty(required=False)

    @classmethod
    def generate_id(cls):
        return hex(random.getrandbits(160))[2:-1]

    @classmethod
    def get_form(cls, form_unique_id, and_app=False):
        d = get_db().view('app_manager/xforms_index', key=form_unique_id).one()['value']
        # unpack the dict into variables app_id, module_id, form_id
        app_id, unique_id = [d[key] for key in ('app_id', 'unique_id')]

        app = Application.get(app_id)
        form = app.get_form(unique_id)
        if and_app:
            return form, app
        else:
            return form
    def wrapped_xform(self):
        return XForm(self.source)
    def validate_form(self):
        if self.validation_cache is None:
            try:
                XForm(self.source).validate()
            except XFormValidationError as e:
                self.validation_cache = unicode(e)
            else:
                self.validation_cache = ""
            self.get_app().save(increment_version=False)
        if self.validation_cache:
            raise XFormValidationError(self.validation_cache)
        return self
    def get_unique_id(self):
        """
        Return unique_id if it exists, otherwise initialize it

        Does _not_ force a save, so it's the caller's responsibility to save the app

        """
        if not self.unique_id:
            self.unique_id = FormBase.generate_id()
        return self.unique_id

    def get_app(self):
        return self._app

    def get_case_type(self):
        return self._parent.case_type


    def add_stuff_to_xform(self, xform):
        xform.set_default_language(self.get_app().langs[0])
        xform.set_version(self.get_app().version)

    def render_xform(self):
        xform = XForm(self.source)
        self.add_stuff_to_xform(xform)
        return xform.render()

    def _get_active_actions(self, types):
        actions = {}
        for action_type in types:
            a = getattr(self.actions, action_type)
            if a.is_active():
                actions[action_type] = a
        return actions

    def active_actions(self):
        return self._get_active_actions((
            'open_case', 'update_case', 'close_case',
            'open_referral', 'update_referral', 'close_referral',
            'case_preload', 'referral_preload'
        ))

    def active_non_preloader_actions(self):
        return self._get_active_actions((
            'open_case', 'update_case', 'close_case',
            'open_referral', 'update_referral', 'close_referral'))

    def get_questions(self, langs):
        return XForm(self.source).get_questions(langs)

    def export_json(self, dump_json=True):
        source = self.to_json()
        del source['unique_id']
        return json.dumps(source) if dump_json else source
    def rename_lang(self, old_lang, new_lang):
        _rename_key(self.name, old_lang, new_lang)

    def rename_xform_language(self, old_code, new_code):
        source = XForm(self.source)
        source.rename_language(old_code, new_code)
        source = source.render()
        self.source = source

    def check_actions(self):
        errors = []
        # reserved_words are hard-coded in three different places! Very lame of me
        # Here, casexml.js, and module_view.html
        reserved_words = load_case_reserved_words()
        for key in self.actions['update_case'].update:
            if key in reserved_words:
                errors.append({'type': 'update_case uses reserved word', 'word': key})
            if not re.match(r'^[a-zA-Z][\w_-]*$', key):
                errors.append({'type': 'update_case word illegal', 'word': key})
        try:
            valid_paths = set([question['value'] for question in self.get_questions(langs=[])])
        except XFormError as e:
            errors.append({'type': 'invalid xml', 'message': unicode(e)})
        else:
            paths = set()
            for _, action in self.active_actions().items():
                if action.condition.type == 'if':
                    paths.add(action.condition.question)
                if hasattr(action, 'name_path'):
                    paths.add(action.name_path)

            if self.actions.update_case.is_active():
                for _, path in self.actions.update_case.update.items():
                    paths.add(path)
            if self.actions.case_preload.is_active():
                for path, _ in self.actions.case_preload.preload.items():
                    paths.add(path)

            for path in paths:
                if path not in valid_paths:
                    errors.append({'type': 'path error', 'path': path})

        return errors

    def set_requires(self, requires):
        if requires == "none":
            self.actions.update_referral = DocumentSchema()
            self.actions.close_case = DocumentSchema()
            self.actions.close_referral = DocumentSchema()
            self.actions.case_preload = DocumentSchema()
            self.actions.referral_preload = DocumentSchema()
        elif requires == "case":
            self.actions.open_case = DocumentSchema()
            self.actions.close_referral= DocumentSchema()
            self.actions.update_referral = DocumentSchema()
            self.actions.referral_preload = DocumentSchema()
        elif requires == "referral":
            self.actions.open_case = DocumentSchema()
            self.actions.open_referral = DocumentSchema()

        self.requires = requires

    def requires_case(self):
        # all referrals also require cases
        return self.requires in ("case", "referral")

    def requires_case_type(self):
        return self.requires_case() or \
               bool(self.active_non_preloader_actions())

    def requires_referral(self):
        return self.requires == "referral"

class JRResourceProperty(StringProperty):
    def validate(self, value, required=True):
        super(JRResourceProperty, self).validate(value, required)
        if value is not None and not value.startswith('jr://'):
            raise BadValueError("JR Resources must start with 'jr://")
        return value
    
class NavMenuItemMediaMixin(DocumentSchema):
    media_image = JRResourceProperty(required=False)
    media_audio = JRResourceProperty(required=False)


class Form(FormBase, IndexedSchema, NavMenuItemMediaMixin):
    def add_stuff_to_xform(self, xform):
        super(Form, self).add_stuff_to_xform(xform)
        xform.add_case_and_meta(self)
    def get_app(self):
        return self._parent._parent
    def get_module(self):
        return self._parent

class UserRegistrationForm(FormBase):
    username_path = StringProperty(default='username')
    password_path = StringProperty(default='password')
    data_paths = DictProperty()

    def add_stuff_to_xform(self, xform):
        super(UserRegistrationForm, self).add_stuff_to_xform(xform)
        xform.add_user_registration(self.username_path, self.password_path, self.data_paths)

class DetailColumn(IndexedSchema):
    """
    Represents a column in case selection screen on the phone. Ex:
        {
            'header': {'en': 'Sex', 'pt': 'Sexo'},
            'model': 'cc_pf_client',
            'field': 'sex',
            'format': 'enum',
            'enum': {'en': {'m': 'Male', 'f': 'Female'}, 'pt': {'m': 'Macho', 'f': 'Fêmea'}}
        }

    """
    header      = DictProperty()
    model       = StringProperty()
    field       = StringProperty()
    format      = StringProperty()

    enum        = DictProperty()
    late_flag   = IntegerProperty(default=30)
    advanced    = StringProperty(default="")

    def rename_lang(self, old_lang, new_lang):
        for dct in (self.header, self.enum):
            _rename_key(dct, old_lang, new_lang)

    @property
    def xpath(self):
        """
        Convert special names like date-opened to their casedb xpath equivalent (e.g. @date_opened).
        Only ever called by 2.0 apps.

        """
        return {
            'external-id': 'external_id',
            'date-opened': 'date_opened',
            'status': '@status',
            'name': 'case_name',
        }.get(self.field, self.field)
class Detail(DocumentSchema):
    """
    Full configuration for a case selection screen

    """
    type = StringProperty(choices=DETAIL_TYPES)
    columns = SchemaListProperty(DetailColumn)


    def get_columns(self):
        l = len(self.columns)
        for i, column in enumerate(self.columns):
            yield column.with_id(i%l, self)
    @parse_int([1])
    def get_column(self, i):
        return self.columns[i].with_id(i%len(self.columns), self)

    def append_column(self, column):
        self.columns.append(column)
    def update_column(self, column_id, column):
        my_column = self.columns[column_id]

        my_column.model  = column.model
        my_column.field  = column.field
        my_column.format = column.format
        my_column.late_flag = column.late_flag
        my_column.advanced = column.advanced

        for lang in column.header:
            my_column.header[lang] = column.header[lang]

        for key in column.enum:
            for lang in column.enum[key]:
                if key not in my_column.enum:
                    my_column.enum[key] = {}
                my_column.enum[key][lang] = column.enum[key][lang]

    def delete_column(self, column_id):
        del self.columns[column_id]

    def rename_lang(self, old_lang, new_lang):
        for column in self.columns:
            column.rename_lang(old_lang, new_lang)

    @property
    def display(self):
        return "short" if self.type.endswith('short') else 'long'

    def filter_xpath(self):
        filters = []
        for i,column in enumerate(self.columns):
            if column.format == 'filter':
                filters.append("(%s)" % column.filter_xpath.replace('.', '%s_%s_%s' % (column.model, column.field, i + 1)))
        return ' && '.join(filters)

    def filter_xpath_2(self):
        filters = []
        for i,column in enumerate(self.columns):
            if column.format == 'filter':
                filters.append("(%s)" % column.filter_xpath.replace('.', column.xpath))
        if filters:
            return '[%s]' % (' && '.join(filters))
        else:
            return ''

class CaseList(IndexedSchema):
    label = DictProperty()
    show = BooleanProperty(default=False)

    def rename_lang(self, old_lang, new_lang):
        for dct in (self.label,):
            _rename_key(dct, old_lang, new_lang)

class Module(IndexedSchema, NavMenuItemMediaMixin):
    """
    A group of related forms, and configuration that applies to them all.
    Translates to a top-level menu on the phone.

    """
    name = DictProperty()
    case_label = DictProperty()
    referral_label = DictProperty()
    forms = SchemaListProperty(Form)
    details = SchemaListProperty(Detail)
    case_type = StringProperty()
    put_in_root = BooleanProperty(default=False)
    case_list = SchemaProperty(CaseList)
    referral_list = SchemaProperty(CaseList)

    def rename_lang(self, old_lang, new_lang):
        _rename_key(self.name, old_lang, new_lang)
        for form in self.forms:
            form.rename_lang(old_lang, new_lang)
        for detail in self.details:
            detail.rename_lang(old_lang, new_lang)
        for case_list in (self.case_list, self.referral_list):
            case_list.rename_lang(old_lang, new_lang)

    def get_forms(self):
        self__forms = self.forms
        l = len(self__forms)
        for i, form in enumerate(self__forms):
            yield form.with_id(i%l, self)
    @parse_int([1])
    def get_form(self, i):
        self__forms = self.forms
        return self__forms[i].with_id(i%len(self.forms), self)

    def get_detail(self, detail_type):
        for detail in self.details:
            if detail.type == detail_type:
                return detail
        raise Exception("Module %s has no detail type %s" % (self, detail_type))

    def export_json(self, dump_json=True):
        source = self.to_json()
        for form in source['forms']:
            del form['unique_id']
        return json.dumps(source) if dump_json else source
    def requires(self):
        r = set(["none"])
        for form in self.get_forms():
            r.add(form.requires)
        if self.case_list.show:
            r.add('case')
        if self.referral_list.show:
            r.add('referral')
        for val in ("referral", "case", "none"):
            if val in r:
                return val
    def detail_types(self):
        return {
            "referral": ["case_short", "case_long", "ref_short", "ref_long"],
            "case": ["case_short", "case_long"],
            "none": []
        }[self.requires()]

class VersioningError(Exception):
    """For errors that violate the principals of versioning in VersionedDoc"""
    pass

class VersionedDoc(Document):
    """
    A document that keeps an auto-incrementing version number, knows how to make copies of itself,
    delete a copy of itself, and revert back to an earlier copy of itself.

    """
    domain = StringProperty()
    copy_of = StringProperty()
    version = IntegerProperty()
    short_url = StringProperty()

    _meta_fields = ['_id', '_rev', 'domain', 'copy_of', 'version', 'short_url']

    @property
    def id(self):
        return self._id

    def save(self, response_json=None, increment_version=True, **params):
        if increment_version:
            self.version = self.version + 1 if self.version else 1
        super(VersionedDoc, self).save()
        if response_json is not None:
            if 'update' not in response_json:
                response_json['update'] = {}
            response_json['update']['app-version'] = self.version
    def save_copy(self):
        cls = self.__class__
        copies = cls.view('app_manager/applications', key=[self.domain, self._id, self.version], include_docs=True).all()
        if copies:
            copy = copies[0]
        else:
            copy = deepcopy(self.to_json())
            del copy['_id']
            del copy['_rev']
            if 'short_url' in copy:
                del copy['short_url']
            if "recipients" in copy:
                del copy['recipients']
            if '_attachments' in copy:
                del copy['_attachments']
            copy = cls.wrap(copy)
            copy['copy_of'] = self._id
            copy.save(increment_version=False)
            copy.copy_attachments(self, r'.*\.xml')
        return copy

    def copy_attachments(self, other, regexp=None):
        for name in other._attachments or {}:
            if regexp is None or re.match(regexp, name):
                self.put_attachment(other.fetch_attachment(name), name)
    def revert_to_copy(self, copy):
        """
        Replaces couch doc with a copy of the backup ("copy").
        Returns the another Application/RemoteApp referring to this
        updated couch doc. The returned doc should be used in place of
        the original doc, i.e. should be called as follows:
            app = revert_to_copy(app, copy)
        This is not ideal :(
        """
        if copy.copy_of != self._id:
            raise VersioningError("%s is not a copy of %s" % (copy, self))
        app = deepcopy(copy.to_json())
        app['_rev'] = self._rev
        app['_id'] = self._id
        app['version'] = self.version
        app['copy_of'] = None
        if '_attachments' in app:
            del app['_attachments']
        cls = self.__class__
        app = cls.wrap(app)
        app.save()
        app.copy_attachments(copy)
        app.delete_attachment('CommCare.jar')
        app.delete_attachment('CommCare.jad')
        return app

    def delete_copy(self, copy):
        if copy.copy_of != self._id:
            raise VersioningError("%s is not a copy of %s" % (copy, self))
        copy.delete()

    def scrub_source(self, source):
        """
        To be overridden.

        Use this to scrub out anything
        that should be shown in the
        application source, such as ids, etc.

        """
        raise NotImplemented()

    def export_json(self, dump_json=True):
        source = deepcopy(self.to_json())
        for field in self._meta_fields:
            if field in source:
                del source[field]
        _attachments = {}
        for name in source.get('_attachments', {}):
            if name.endswith('.xml', 1):
                _attachments[name] = self.fetch_attachment(name)
        source['_attachments'] = _attachments
        self.scrub_source(source)

        return json.dumps(source) if dump_json else source
    @classmethod
    def from_source(cls, source, domain):
        for field in cls._meta_fields:
            if field in source:
                del source[field]
        source['domain'] = domain
        app = cls.wrap(source)
        return app

    def is_deleted(self):
        return self.doc_type.endswith(DELETED_SUFFIX)

    def unretire(self):
        self.doc_type = self.get_doc_type()
        self.save()

    def get_doc_type(self):
        if self.doc_type.endswith(DELETED_SUFFIX):
            return self.doc_type[:-len(DELETED_SUFFIX)]
        else:
            return self.doc_type


class ApplicationBase(VersionedDoc):
    """
    Abstract base class for Application and RemoteApp.
    Contains methods for generating the various files and zipping them into CommCare.jar

    """

    recipients = StringProperty(default="")

    # this is the supported way of specifying which commcare build to use
    build_spec = SchemaProperty(BuildSpec)
    text_input = StringProperty(choices=['roman', 'native', 'custom-keys'], default="roman")
    success_message = DictProperty()

    # The following properties should only appear on saved builds
    # built_with stores a record of CommCare build used in a saved app
    built_with = SchemaProperty(BuildRecord)
    build_signed = BooleanProperty(default=True)
    built_on = DateTimeProperty(required=False)
    build_comment = StringProperty()

    # django-style salted hash of the admin password
    admin_password = StringProperty()
    # a=Alphanumeric, n=Numeric, x=Neither (not allowed)
    admin_password_charset = StringProperty(choices=['a', 'n', 'x'], default='n')

    # This is here instead of in Application because it needs to be available in stub representation
    application_version = StringProperty(default=APP_V1, choices=[APP_V1, APP_V2], required=False)

    @classmethod
    def wrap(cls, data):
        # scrape for old conventions and get rid of them
        if data.has_key("commcare_build"):
            version, build_number = data['commcare_build'].split('/')
            data['build_spec'] = BuildSpec.from_string("%s/latest" % version).to_json()
            del data['commcare_build']
        if data.has_key("commcare_tag"):
            version, build_number = current_builds.TAG_MAP[data['commcare_tag']]
            data['build_spec'] = BuildSpec.from_string("%s/latest" % version).to_json()
            del data['commcare_tag']
        if data.has_key("built_with") and isinstance(data['built_with'], basestring):
            data['built_with'] = BuildSpec.from_string(data['built_with']).to_json()

        if data.has_key('native_input'):
            if not data.has_key('text_input'):
                data['text_input'] = 'native' if data['native_input'] else 'roman'
            del data['native_input']

        self = super(ApplicationBase, cls).wrap(data)
        if not self.build_spec or self.build_spec.is_null():
            self.build_spec = CommCareBuildConfig.fetch().get_default(self.application_version)
        return self

    def is_remote_app(self):
        return False

    def set_admin_password(self, raw_password):
        import random
        algo = 'sha1'
        salt = get_hexdigest(algo, str(random.random()), str(random.random()))[:5]
        hsh = get_hexdigest(algo, salt, raw_password)
        self.admin_password = '%s$%s$%s' % (algo, salt, hsh)

        if raw_password.isnumeric():
            self.admin_password_charset = 'n'
        elif raw_password.isalnum():
            self.admin_password_charset = 'a'
        else:
            self.admin_password_charset = 'x'

    def check_password_charset(self):
        errors = []
        if hasattr(self, 'profile'):
            password_format = self.profile.get('properties', {}).get('password_format', 'n')
            message = 'Your app requires {0} passwords but the admin password is not {0}'

            if password_format == 'n' and self.admin_password_charset in 'ax':
                errors.append({'type': 'password_format', 'message': message.format('numeric')})
            if password_format == 'a' and self.admin_password_charset in 'x':
                errors.append({'type': 'password_format', 'message': message.format('alphanumeric')})
        return errors

    def get_build(self):
#        version, build_number = current_builds.TAG_MAP[self.commcare_tag]
#        return CommCareBuild.get_build(version, build_number)
        return self.build_spec.get_build()

    def get_preview_build(self):
        preview = self.get_build()

        for path in getattr(preview, '_attachments', {}):
            if path.startswith('Generic/WebDemo'):
                return preview
        return CommCareBuildConfig.fetch().preview.get_build()

    @property
    def commcare_minor_release(self):
        """This is mostly just for views"""
        return self.build_spec.minor_release()

    def get_build_label(self):
        """This is a helper to look up a human readable name for a build tag"""
#        for option in current_builds.MENU_OPTIONS:
#            if option['tag'] == self.commcare_tag:
#                return option['label']
        for item in CommCareBuildConfig.fetch().menu:
            if item['build'].to_string() == self.build_spec.to_string():
                return item['label']
        return self.build_spec.get_label()

    @property
    def post_url(self):
        return "%s%s" % (
            get_url_base(),
            reverse('corehq.apps.receiverwrapper.views.post', args=[self.domain])
        )
    @property
    def ota_restore_url(self):
        return "%s%s" % (
            get_url_base(),
            reverse('corehq.apps.ota.views.restore', args=[self.domain])
        )
    @property
    def profile_url(self):
        return "%s%s?latest=true" % (
            get_url_base(),
            reverse('download_profile', args=[self.domain, self.copy_of or self._id])
        )
    @property
    def profile_loc(self):
        return "jr://resource/profile.xml"
    @property
    def jar_url(self):
        return "%s%s" % (
            get_url_base(),
            reverse('corehq.apps.app_manager.views.download_jar', args=[self.domain, self._id]),
        )
    
    def get_jadjar(self):
        build = self.get_build()
        if self.text_input == 'custom-keys' and build.minor_release() < (1,3):
            raise AppError("Custom Keys not supported in CommCare versions before 1.3. (Using %s.%s)" % build.minor_release())

        spec = {
            ('native',): 'Nokia/S40-native-input',
            ('roman',): 'Nokia/S40-generic',
            ('custom-keys',):  'Nokia/S40-custom-keys',
        }[(self.text_input,)]
        return self.get_build().get_jadjar(spec)

    def create_jadjar(self):
        try:
            return self.fetch_attachment('CommCare.jad'), self.fetch_attachment('CommCare.jar')
        except ResourceError:
            built_on = datetime.utcnow()
            jadjar = self.get_jadjar().pack(self.create_all_files(), {
                'JavaRosa-Admin-Password': self.admin_password,
                'Profile': self.profile_loc,
                'MIDlet-Jar-URL': self.jar_url,
                #'MIDlet-Name': self.name,
                # e.g. 2011-Apr-11 20:45
                'Released-on': built_on.strftime("%Y-%b-%d %H:%M"),
                'CommCare-Release': "true",
                'Build-Number': self.version,
            })
            self.put_attachment(jadjar.jad, 'CommCare.jad')
            self.put_attachment(jadjar.jar, 'CommCare.jar')
            self.built_on = built_on
            self.built_with = BuildRecord(
                version=jadjar.version,
                build_number=jadjar.build_number,
                signed=jadjar.signed,
                datetime=built_on,
            )
            self.save(increment_version=False)
            return jadjar.jad, jadjar.jar

    def validate_app(self):
        errors = []

        errors.extend(self.check_password_charset())

        try:
            self.create_all_files()
        except (AppError, XFormValidationError) as e:
            errors.append({'type': 'error', 'message': unicode(e)})
        except Exception as e:
            errors.append({'type': 'error', 'message': 'unexpected error: %s' % e})
        return errors

    @property
    def odk_profile_url(self):

        return "%s%s" % (
            get_url_base(),
            reverse('corehq.apps.app_manager.views.download_odk_profile', args=[self.domain, self._id]),
        )

    def get_odk_qr_code(self):
        """Returns a QR code, as a PNG to install on CC-ODK"""
        try:
            return self.fetch_attachment("qrcode.png")
        except ResourceNotFound:
            try:
                from pygooglechart import QRChart
            except ImportError:
                raise Exception(MISSING_DEPENDECY)
            HEIGHT = WIDTH = 250
            code = QRChart(HEIGHT, WIDTH)
            code.add_data(self.odk_profile_url)

            # "Level H" error correction with a 0 pixel margin
            code.set_ec('H', 0)
            f, fname = tempfile.mkstemp()
            code.download(fname)
            os.close(f)
            with open(fname, "rb") as f:
                png_data = f.read()
                self.put_attachment(png_data, "qrcode.png", content_type="image/png")
            return png_data

    def fetch_jar(self):
        return self.get_jadjar().fetch_jar()

    def fetch_emulator_commcare_jar(self):
        path = "Generic/WebDemo"
        jadjar = self.get_preview_build().get_jadjar(path)
        jadjar = jadjar.pack(self.create_all_files())
        return jadjar.jar

    def save_copy(self, comment=None):
        copy = super(ApplicationBase, self).save_copy()

        copy.create_jadjar()

        try:
            copy.short_url = bitly.shorten(
                get_url_base() + reverse('corehq.apps.app_manager.views.download_jad', args=[copy.domain, copy._id])
            )
        except (URLError, Exception):
            # for offline only
            logging.exception("Problem creating bitly url for app %s. Do you have network?" % self.get_id)
            copy.short_url = None
        copy.build_comment = comment
        copy.save(increment_version=False)

        return copy

    def delete_app(self):
        self.doc_type += '-Deleted'
        record = DeleteApplicationRecord(
            domain=self.domain,
            app_id=self.id,
            datetime=datetime.utcnow()
        )
        record.save()
        return record
#class Profile(DocumentSchema):
#    features = DictProperty()
#    properties = DictProperty()

def validate_lang(lang):
    if not re.match(r'^[a-z]{2,3}(-[a-z]*)?$', lang):
        raise ValueError("Invalid Language")

class Application(ApplicationBase, TranslationMixin, HQMediaMixin):
    """
    A Managed Application that can be created entirely through the online interface, except for writing the
    forms themselves.

    """
    user_registration = SchemaProperty(UserRegistrationForm)
    show_user_registration = BooleanProperty(default=False, required=True)
    modules = SchemaListProperty(Module)
    name = StringProperty()
    langs = StringListProperty()
    profile = DictProperty() #SchemaProperty(Profile)
    use_custom_suite = BooleanProperty(default=False)
    force_http = BooleanProperty(default=False)
    cloudcare_enabled = BooleanProperty(default=False)
    
    @classmethod
    def wrap(cls, data):
        for module in data['modules']:
            for attr in ('case_label', 'referral_label'):
                if not module.has_key(attr):
                    module[attr] = {}
            for lang in data['langs']:
                if not module['case_label'].get(lang):
                    module['case_label'][lang] = commcare_translations.load_translations(lang).get('cchq.case', 'Cases')
                if not module['referral_label'].get(lang):
                    module['referral_label'][lang] = commcare_translations.load_translations(lang).get('cchq.referral', 'Referrals')
        return super(Application, cls).wrap(data)

    def register_pre_save(self, fn):
        if not hasattr(self, '_PRE_SAVE'):
            self._PRE_SAVE = []
        self._PRE_SAVE.append(fn)

    def register_post_save(self, fn):
        if not hasattr(self, '_POST_SAVE'):
            self._POST_SAVE = []
        self._POST_SAVE.append(fn)

    def save(self, response_json=None, **kwargs):
        if hasattr(self, '_PRE_SAVE'):
            for pre_save in self._PRE_SAVE:
                pre_save()
            def del_pre_save():
                del self._PRE_SAVE
            self.register_post_save(del_pre_save)

        super(Application, self).save(response_json, **kwargs)
        if hasattr(self, '_POST_SAVE'):
            for post_save in self._POST_SAVE:
                post_save()

            del self._POST_SAVE

    @property
    def url_base(self):
        if self.force_http:
            return settings.INSECURE_URL_BASE
        else:
            return get_url_base()

    @property
    def post_url(self):
        return "%s%s" % (
            self.url_base,
            reverse('receiver_post_with_app_id', args=[self.domain, self.copy_of or self.get_id])
        )
    @property
    def ota_restore_url(self):
        return "%s%s" % (
            self.url_base,
            reverse('corehq.apps.ota.views.restore', args=[self.domain])
        )

    @property
    def suite_url(self):
        return "%s%s?latest=true" % (
            self.url_base,
            reverse('download_suite', args=[self.domain, self.copy_of or self.get_id])
        )
    @property
    def suite_loc(self):
        return "suite.xml"
#    @property
#    def jar_url(self):
#        return "%s%s" % (
#            get_url_base(),
#            reverse('corehq.apps.app_manager.views.download_zipped_jar', args=[self.domain, self._id]),
#        )
    def fetch_xform(self, module_id, form_id):
        form = self.get_module(module_id).get_form(form_id)
        return form.validate_form().render_xform()

    def create_app_strings(self, lang, template='app_manager/app_strings.txt'):
        def non_empty_only(dct):
            return dict([(key, value) for key, value in dct.items() if value])
        if lang != "default":
            messages = {"cchq.case": "Case", "cchq.referral": "Referral"}

            custom = render_to_string(template, {
                'app': self,
                'langs': [lang] + self.langs,
            })
            custom = commcare_translations.loads(custom)
            messages.update(non_empty_only(custom))

            # include language code names
            for lc in self.langs:
                name = langcodes.get_name(lc) or lc
                if name:
                    messages[lc] = name

            cc_trans = commcare_translations.load_translations(lang)
            messages.update(cc_trans)

            messages.update(non_empty_only(self.translations.get(lang, {})))
        else:
            messages = {}
            for lc in reversed(self.langs):
                if lc == "default": continue
                messages.update(
                    commcare_translations.loads(self.create_app_strings(lc).encode('utf-8'))
                )
        return commcare_translations.dumps(messages)


    def create_profile(self, is_odk=False, template='app_manager/profile.xml'):
        app_profile = defaultdict(dict)
        app_profile.update(self.profile)
        # the following code is to let HQ override CommCare defaults
        # impetus: Weekly Logging should be Short (HQ override) instead of Never (CommCare default)
        # property.default is assumed to also be the CommCare default unless there's a property.commcare_default
        all_properties = load_custom_commcare_properties()
        for property in all_properties:
            type = property.get('type', 'properties')
            if property['id'] not in app_profile[type]:
                if property.has_key('commcare_default') and property['commcare_default'] != property['default']:
                    app_profile[type][property['id']] = property['default']

        return render_to_string(template, {
            'is_odk': is_odk,
            'app': self,
            'app_profile': app_profile,
            'suite_url': self.suite_url,
            'suite_loc': self.suite_loc,
            'post_url': self.post_url,
            'post_test_url': self.post_url,
            'ota_restore_url': self.ota_restore_url,
            'cc_user_domain': cc_user_domain(self.domain)
        }).decode('utf-8')

    @property
    def custom_suite(self):
        try:
            return self.fetch_attachment('custom_suite.xml')
        except Exception:
            return ""

    def set_custom_suite(self, value):
        self.put_attachment(value, 'custom_suite.xml')

    def create_suite(self):
        template='app_manager/suite-%s.xml' % self.application_version

        return render_to_string(template, {
            'app': self,
            'langs': ["default"] + self.langs
        })

    def create_all_files(self):
        files = {
            "profile.xml": self.create_profile(),
            "suite.xml": self.create_suite(),
        }
        if self.show_user_registration:
            files["user_registration.xml"] = self.get_user_registration().validate_form().render_xform()
        for lang in ['default'] + self.langs:
            files["%s/app_strings.txt" % lang] = self.create_app_strings(lang)
        for module in self.get_modules():
            for form in module.get_forms():
                files["modules-%s/forms-%s.xml" % (module.id, form.id)] = self.fetch_xform(module.id, form.id)
        return files

    def get_modules(self):
        self__modules = self.modules
        l = len(self__modules)
        for i,module in enumerate(self__modules):
            yield module.with_id(i%l, self)

    @parse_int([1])
    def get_module(self, i):
        self__modules = self.modules
        return self__modules[i].with_id(i%len(self__modules), self)

    def get_user_registration(self):
        form = self.user_registration
        form._app = self
        if not form.source:
            form.source = load_default_user_registration().format(user_registration_xmlns="%s%s" % (
                get_url_base(),
                reverse('view_user_registration', args=[self.domain, self.id])
            ))
        return form

    def get_forms(self, bare=True):
        if self.show_user_registration:
            yield self.get_user_registration() if bare else {
                'type': 'user_registration',
                'form': self.get_user_registration()
            }
        for module in self.get_modules():
            for form in module.get_forms():
                yield form if bare else {
                    'type': 'module_form',
                    'module': module,
                    'form': form
                }

    def get_form(self, unique_form_id, bare=True):
        def matches(form):
            return form.get_unique_id() == unique_form_id
        for obj in self.get_forms(bare):
            if matches(obj if bare else obj['form']):
                return obj
        raise KeyError("Form in app '%s' with unique id '%s' not found" % (self.id, unique_form_id))

    @classmethod
    def new_app(cls, domain, name, application_version, lang="en"):
        app = cls(domain=domain, modules=[], name=name, langs=[lang], application_version=application_version)
        return app

    def new_module(self, name, lang):
        self.modules.append(
            Module(
                name={lang if lang else "en": name if name else "Untitled Module"},
                forms=[],
                case_type='',
                details=[Detail(type=detail_type, columns=[]) for detail_type in DETAIL_TYPES],
            )
        )
        return self.get_module(-1)

    def new_module_from_source(self, source):
        self.modules.append(Module.wrap(source))
        return self.get_module(-1)

    @parse_int([1])
    def delete_module(self, module_id):
        module = self.modules[module_id]
        record = DeleteModuleRecord(
            domain=self.domain,
            app_id=self.id,
            module_id=module_id,
            module=module,
            datetime=datetime.utcnow()
        )
        del self.modules[module_id]
        record.save()
        return record

    def new_form(self, module_id, name, lang, attachment=""):
        module = self.get_module(module_id)
        form = Form(
            name={lang if lang else "en": name if name else "Untitled Form"},
        )
        module.forms.append(form)
        form = module.get_form(-1)
        form.source = attachment
        return form

    def new_form_from_source(self, module_id, source):
        module = self.get_module(module_id)
        module.forms.append(Form.wrap(source))
        form = module.get_form(-1)
        return form
    @parse_int([1, 2])
    def delete_form(self, module_id, form_id):
        module = self.get_module(module_id)
        form = module['forms'][form_id]
        record = DeleteFormRecord(
            domain=self.domain,
            app_id=self.id,
            module_id=module_id,
            form_id=form_id,
            form=form,
            datetime=datetime.utcnow()
        )
        record.save()
        del module['forms'][form_id]
        return record

    def rename_lang(self, old_lang, new_lang):
        validate_lang(new_lang)
        if old_lang == new_lang:
            return
        if new_lang in self.langs:
            raise AppError("Language %s already exists!" % new_lang)
        for i,lang in enumerate(self.langs):
            if lang == old_lang:
                self.langs[i] = new_lang
        for module in self.modules:
            module.rename_lang(old_lang, new_lang)
        _rename_key(self.translations, old_lang, new_lang)


    def rearrange_langs(self, i, j):
        langs = self.langs
        langs.insert(i, langs.pop(j))
        self.langs = langs
    def rearrange_modules(self, i, j):
        modules = self.modules
        modules.insert(i, modules.pop(j))
        self.modules = modules
    def rearrange_detail_columns(self, module_id, detail_type, i, j):
        module = self.get_module(module_id)
        detail = module['details'][DETAIL_TYPES.index(detail_type)]
        columns = detail['columns']
        columns.insert(i, columns.pop(j))
        detail['columns'] = columns
    def rearrange_forms(self, module_id, i, j):
        forms = self.modules[module_id]['forms']
        forms.insert(i, forms.pop(j))
        self.modules[module_id]['forms'] = forms
    def scrub_source(self, source):
        def change_unique_id(form):
            unique_id = form['unique_id']
            new_unique_id = FormBase.generate_id()
            form['unique_id'] = new_unique_id
            if source['_attachments'].has_key("%s.xml" % unique_id):
                source['_attachments']["%s.xml" % new_unique_id] = source['_attachments'].pop("%s.xml" % unique_id)

        change_unique_id(source['user_registration'])
        for m,module in enumerate(source['modules']):
            for f,form in enumerate(module['forms']):
                change_unique_id(source['modules'][m]['forms'][f])


    def get_xmlns_map(self):
        map = defaultdict(list)
        for form in self.get_forms():
            map[form.xmlns].append(form)
        return map

    def validate_app(self):
        xmlns_count = defaultdict(int)
        errors = []
        if not self.modules:
            errors.append({"type": "no modules"})
        for module in self.get_modules():
            if not module.forms:
                errors.append({'type': "no forms", "module": {"id": module.id, "name": module.name}})

        for obj in self.get_forms(bare=False):
            needs_case_type = False
            needs_case_detail = False
            needs_referral_detail = False

            module = obj.get('module')
            form = obj.get('form')
            form_type = obj.get('type')
            meta = dict(
                form_type=form_type,
                module={"id": module.id, "name": module.name} if module else {},
                form={"id": form.id if hasattr(form, 'id') else None, "name": form.name}
            )
            errors_ = form.check_actions()
            for error_ in errors_:
                error_.update(meta)
            errors.extend(errors_)

            try:
                _parse_xml(form.source)
            except XFormError as e:
                errors.append(dict(
                    type="invalid xml",
                    message=unicode(e),
                    **meta
                ))
            except ValueError:
                logging.error("Failed: _parse_xml(string=%r)" % form.source)
                raise
            xmlns_count[form.xmlns] += 1
            if form.requires_case():
                needs_case_detail = True
                needs_case_type = True
            if form.requires_case_type():
                needs_case_type = True
            if form.requires_referral():
                needs_referral_detail = True

            if module:
                if needs_case_type and not module.case_type:
                    errors.append({'type': "no case type", "module": {"id": module.id, "name": module.name}})
                if needs_case_detail and not module.get_detail('case_short').columns:
                    errors.append({'type': "no case detail", "module": {"id": module.id, "name": module.name}})
                if needs_referral_detail and not module.get_detail('ref_short').columns:
                    errors.append({'type': "no ref detail", "module": {"id": module.id, "name": module.name}})

            # make sure that there aren't duplicate xmlns's
            for xmlns in xmlns_count:
                if xmlns_count[xmlns] > 1:
                    errors.append({'type': "duplicate xmlns", "xmlns": xmlns})

        if not errors:
            errors = super(Application, self).validate_app()
        return errors

    @classmethod
    def get_by_xmlns(cls, domain, xmlns):
        r = get_db().view('reports/forms_by_xmlns', key=[domain, {}, xmlns], group=True).one()
        return cls.get(r['value']['app']['id']) if r and 'app' in r['value'] else None

class NotImplementedYet(Exception):
    pass
class RemoteApp(ApplicationBase):
    """
    A wrapper for a url pointing to a suite or profile file. This allows you to
    write all the files for an app by hand, and then give the url to app_manager
    and let it package everything together for you.

    """
    profile_url = StringProperty(default="http://")
    name = StringProperty()

    # @property
    #     def suite_loc(self):
    #         if self.suite_url:
    #             return self.suite_url.split('/')[-1]
    #         else:
    #             raise NotImplementedYet()

    def is_remote_app(self):
        return True

    @classmethod
    def new_app(cls, domain, name, lang='en'):
        app = cls(domain=domain, name=name, langs=[lang])
        return app

    # def fetch_suite(self):
    #     return urlopen(self.suite_url).read()
    def create_profile(self, is_odk=False):
        # we don't do odk for now anyway
        try:
            return urlopen(self.profile_url).read()
        except Exception:
            raise AppError('Unable to access profile url: "%s"' % self.profile_url)

    def fetch_file(self, location):
        base = '/'.join(self.profile_url.split('/')[:-1]) + '/'
        if location.startswith('./'):
            location = location.lstrip('./')
        elif location.startswith(base):
            location = location.lstrip(base)
        elif location.startswith('jr://resource/'):
            location = location.lstrip('jr://resource/')
        url = urljoin(self.profile_url, location)
        try:
            return location, urlopen(url).read().decode('utf-8')
        except Exception:
            raise AppError('Unable to access resource url: "%s"' % url)

    def create_all_files(self):
        files = {
            'profile.xml': self.create_profile(),
        }
        tree = _parse_xml(files['profile.xml'])
        suite_loc = tree.find('suite/resource/location[@authority="local"]').text
        suite_loc, suite = self.fetch_file(suite_loc)
        files[suite_loc] = suite
        suite_xml = _parse_xml(suite)

        locations = []
        for resource in suite_xml.findall('*/resource'):
            try:
                loc = resource.findtext('location[@authority="local"]')
            except Exception:
                loc = resource.findtext('location[@authority="remote"]')
            locations.append(loc)
        for location in locations:
            files.update((self.fetch_file(location),))
        return files
    def scrub_source(self, source):
        pass

class DomainError(Exception):
    pass

class AppError(Exception):
    pass

class BuildErrors(Document):

    errors = ListProperty()

def get_app(domain, app_id, wrap_cls=None, latest=False):
    """
    Utility for getting an app, making sure it's in the domain specified, and wrapping it in the right class
    (Application or RemoteApp).

    """

    if latest:
        if not domain:
            try:
                domain = get_db().get(app_id)['domain']
            except Exception:
                raise Http404
        app = get_db().view('app_manager/applications',
            startkey=[domain, app_id, {}],
            limit=1,
            descending=True,
            include_docs=True
        ).one()['doc']
    else:
        try:
            app = get_db().get(app_id)
        except Exception:
            raise Http404

    if domain:
        try:    Domain.get_by_name(domain)
        except: raise Http404

        if app['domain'] != domain:
            raise Http404
    cls = wrap_cls or {
        'Application': Application,
        'Application-Deleted': Application,
        "RemoteApp": RemoteApp,
        "RemoteApp-Deleted": RemoteApp,
    }[app['doc_type']]
    app = cls.wrap(app)
    return app

EXAMPLE_DOMAIN = 'example'
BUG_REPORTS_DOMAIN = 'bug-reports'

def _get_or_create_app(app_id):
    if app_id == "example--hello-world":
        try:
            app = Application.get(app_id)
        except ResourceNotFound:
            app = Application.wrap(fixtures.hello_world_example)
            app._id = app_id
            app.domain = EXAMPLE_DOMAIN
            app.save()
            return _get_or_create_app(app_id)
        return app
    else:
        return get_app(None, app_id)

str_to_cls = {
    "Application": Application,
    "Application-Deleted": Application,
    "RemoteApp": RemoteApp,
    "RemoteApp-Deleted": RemoteApp,
}

def import_app(app_id_or_source, domain, name=None, validate_source_domain=None):
    if isinstance(app_id_or_source, basestring):
        app_id = app_id_or_source
        source = _get_or_create_app(app_id)
        src_dom = source['domain']
        if validate_source_domain:
            validate_source_domain(src_dom)
        source = source.export_json()
        source = json.loads(source)
    else:
        source = app_id_or_source
    try: attachments = source.pop('_attachments')
    except KeyError: attachments = {}
    if name:
        source['name'] = name
    cls = str_to_cls[source['doc_type']]
    app = cls.from_source(source, domain)
    app.save()
    for name, attachment in attachments.items():
        app.put_attachment(attachment, name)
    return app

class DeleteApplicationRecord(DeleteRecord):
    app_id = StringProperty()

    def undo(self):
        app = ApplicationBase.get(self.app_id)
        app.doc_type = app.get_doc_type()
        app.save()

class DeleteModuleRecord(DeleteRecord):
    app_id = StringProperty()
    module_id = IntegerProperty()
    module = SchemaProperty(Module)

    def undo(self):
        app = Application.get(self.app_id)
        modules = app.modules
        modules.insert(self.module_id, self.module)
        app.modules = modules
        app.save()

class DeleteFormRecord(DeleteRecord):
    app_id = StringProperty()
    module_id = IntegerProperty()
    form_id = IntegerProperty()
    form = SchemaProperty(Form)

    def undo(self):
        app = Application.get(self.app_id)
        forms = app.modules[self.module_id].forms
        forms.insert(self.form_id, self.form)
        app.modules[self.module_id].forms = forms
        app.save()

Form.get_command_id = lambda self: "m{module.id}-f{form.id}".format(module=self.get_module(), form=self)
Form.get_locale_id = lambda self: "forms.m{module.id}f{form.id}".format(module=self.get_module(), form=self)

Module.get_locale_id = lambda self: "modules.m{module.id}".format(module=self)

Module.get_case_list_command_id = lambda self: "m{module.id}-case-list".format(module=self)
Module.get_case_list_locale_id = lambda self: "case_lists.m{module.id}".format(module=self)

Module.get_referral_list_command_id = lambda self: "m{module.id}-referral-list".format(module=self)
Module.get_referral_list_locale_id = lambda self: "referral_lists.m{module.id}".format(module=self)
import corehq.apps.app_manager.signals