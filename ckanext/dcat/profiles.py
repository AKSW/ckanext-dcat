import datetime
import json

from dateutil.parser import parse as parse_date

from pylons import config

import rdflib
from rdflib import URIRef, BNode, Literal
from rdflib.namespace import Namespace, RDF, XSD

from ckan.plugins import toolkit

from ckanext.dcat.utils import resource_uri, publisher_uri_from_dataset_dict

DCT = Namespace("http://purl.org/dc/terms/")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
ADMS = Namespace("http://www.w3.org/ns/adms#")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
SCHEMA = Namespace('http://schema.org/')
TIME = Namespace('http://www.w3.org/2006/time')

namespaces = {
    'dct': DCT,
    'dcat': DCAT,
    'adms': ADMS,
    'vcard': VCARD,
    'foaf': FOAF,
    'schema': SCHEMA,
    'time': TIME,
}


class RDFProfile(object):
    '''Base class with helper methods for implementing RDF parsing profiles

       This class should not be used directly, but rather extended to create
       custom profiles
    '''

    def __init__(self, graph, compatibility_mode=False):
        '''Class constructor

        Graph is an rdflib.Graph instance.

        In compatibility mode, some fields are modified to maintain
        compatibility with previous versions of the ckanext-dcat parsers
        (eg adding the `dcat_` prefix or storing comma separated lists instead
        of JSON dumps).
        '''

        self.g = graph

        self.compatibility_mode = compatibility_mode

    def _datasets(self):
        '''
        Generator that returns all DCAT datasets on the graph

        Yields rdflib.term.URIRef objects that can be used on graph lookups
        and queries
        '''
        for dataset in self.g.subjects(RDF.type, DCAT.Dataset):
            yield dataset

    def _distributions(self, dataset):
        '''
        Generator that returns all DCAT distributions on a particular dataset

        Yields rdflib.term.URIRef objects that can be used on graph lookups
        and queries
        '''
        for distribution in self.g.objects(dataset, DCAT.distribution):
            yield distribution

    def _object(self, subject, predicate):
        '''
        Helper for returning the first object for this subject and predicate

        Both subject and predicate must be rdflib URIRef or BNode objects

        Returns an rdflib reference (URIRef or BNode) or None if not found
        '''
        for _object in self.g.objects(subject, predicate):
            return _object
        return None

    def _object_value(self, subject, predicate):
        '''
        Given a subject and a predicate, returns the value of the object

        Both subject and predicate must be rdflib URIRef or BNode objects

        If found, the unicode representation is returned, else None
        '''
        for o in self.g.objects(subject, predicate):
            return unicode(o)
        return None

    def _object_value_int(self, subject, predicate):
        '''
        Given a subject and a predicate, returns the value of the object as an
        integer

        Both subject and predicate must be rdflib URIRef or BNode objects

        If the value can not be parsed as intger, returns None
        '''
        object_value = self._object_value(subject, predicate)
        if object_value:
            try:
                return int(object_value)
            except ValueError:
                pass
        return None

    def _object_value_list(self, subject, predicate):
        '''
        Given a subject and a predicate, returns a list with all the values of
        the objects

        Both subject and predicate must be rdflib URIRef or BNode  objects

        If no values found, returns an empty string
        '''
        return [unicode(o) for o in self.g.objects(subject, predicate)]

    def _time_interval(self, subject, predicate):
        '''
        Returns the start and end date for a time interval object

        Both subject and predicate must be rdflib URIRef or BNode objects

        It checks for time intervals defined with both schema.org startDate &
        endDate and W3C Time hasBeginning & hasEnd.

        Note that partial dates will be expanded to the first month / day
        value, eg '1904' -> '1904-01-01'.

        Returns a tuple with the start and end date values, both of which
        can be None if not found
        '''

        start_date = end_date = None

        for interval in self.g.objects(subject, predicate):
            # Fist try the schema.org way
            start_date = self._object_value(interval, SCHEMA.startDate)
            end_date = self._object_value(interval, SCHEMA.endDate)

            if start_date or end_date:
                return start_date, end_date

            # If no luck, try the w3 time way
            start_nodes = [t for t in self.g.objects(interval,
                                                     TIME.hasBeginning)]
            end_nodes = [t for t in self.g.objects(interval,
                                                   TIME.hasEnd)]
            if start_nodes:
                start_date = self._object_value(start_nodes[0],
                                                TIME.inXSDDateTime)
            if end_nodes:
                end_date = self._object_value(end_nodes[0],
                                              TIME.inXSDDateTime)

        return start_date, end_date

    def _publisher(self, subject, predicate):
        '''
        Returns a dict with details about a dct:publisher entity, a foaf:Agent

        Both subject and predicate must be rdflib URIRef or BNode objects

        Examples:

        <dct:publisher>
            <foaf:Organization rdf:about="http://orgs.vocab.org/some-org">
                <foaf:name>Publishing Organization for dataset 1</foaf:name>
                <foaf:mbox>contact@some.org</foaf:mbox>
                <foaf:homepage>http://some.org</foaf:homepage>
                <dct:type rdf:resource="http://purl.org/adms/publishertype/NonProfitOrganisation"/>
            </foaf:Organization>
        </dct:publisher>

        {
            'uri': 'http://orgs.vocab.org/some-org',
            'name': 'Publishing Organization for dataset 1',
            'email': 'contact@some.org',
            'url': 'http://some.org',
            'type': 'http://purl.org/adms/publishertype/NonProfitOrganisation',
        }

        <dct:publisher rdf:resource="http://publications.europa.eu/resource/authority/corporate-body/EURCOU" />

        {
            'uri': 'http://publications.europa.eu/resource/authority/corporate-body/EURCOU'
        }

        Returns keys for uri, name, email, url and type with the values set to
        None if they could not be found
        '''

        publisher = {}

        for agent in self.g.objects(subject, predicate):

            publisher['uri'] = (str(agent) if isinstance(agent,
                                rdflib.term.URIRef) else None)

            publisher['name'] = self._object_value(agent, FOAF.name)

            publisher['email'] = self._object_value(agent, FOAF.mbox)

            publisher['url'] = self._object_value(agent, FOAF.homepage)

            publisher['type'] = self._object_value(agent, DCT.type)

        return publisher

    def _contact_details(self, subject, predicate):
        '''
        Returns a dict with details about a vcard expression

        Both subject and predicate must be rdflib URIRef or BNode objects

        Returns keys for uri, name and email with the values set to
        None if they could not be found
        '''

        contact = {}

        for agent in self.g.objects(subject, predicate):

            contact['uri'] = (str(agent) if isinstance(agent,
                              rdflib.term.URIRef) else None)

            contact['name'] = self._object_value(agent, VCARD.fn)

            contact['email'] = self._object_value(agent, VCARD.hasEmail)

        return contact

    def _get_dict_value(self, _dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        By default a key on the root level is checked. If not found, extras
        are checked, both with the key provided and with `dcat_` prepended to
        support legacy fields.

        If not found, returns the default value, which defaults to None
        '''

        if key in _dict:
            return _dict[key]

        for extra in _dict.get('extras', []):
            if extra['key'] == key or extra['key'] == 'dcat_' + key:
                return extra['value']

        return default

    def _get_dataset_value(self, dataset_dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        Check `_get_dict_value` for details
        '''
        return self._get_dict_value(dataset_dict, key, default)

    def _get_resource_value(self, resource_dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        Check `_get_dict_value` for details
        '''
        return self._get_dict_value(resource_dict, key, default)

    def _add_date_triples_from_dict(self, _dict, subject, items):
        self._add_triples_from_dict(_dict, subject, items,
                                    date_value=True)

    def _add_list_triples_from_dict(self, _dict, subject, items):
        self._add_triples_from_dict(_dict, subject, items,
                                    list_value=True)

    def _add_triples_from_dict(self, _dict, subject, items,
                               list_value=False,
                               date_value=False):
        for item in items:
            key, predicate, fallbacks = item
            self._add_triple_from_dict(_dict, subject, predicate, key,
                                       fallbacks=fallbacks,
                                       list_value=list_value,
                                       date_value=date_value)

    def _add_triple_from_dict(self, _dict, subject, predicate, key,
                              fallbacks=None,
                              list_value=False,
                              date_value=False):
        '''
        Adds a new triple to the graph with the provided parameters

        The subject and predicate of the triple are passed as the relevant
        RDFLib objects (URIRef or BNode). The object is always a literal value,
        which is extracted from the dict using the provided key (see
        `_get_dict_value`). If the value for the key is not found, then
        additional fallback keys are checked.

        If `list_value` or `date_value` are True, then the value is treated as
        a list or a date respectively (see `_add_list_triple` and
        `_add_date_triple` for details.
        '''
        value = self._get_dict_value(_dict, key)
        if not value and fallbacks:
            for fallback in fallbacks:
                value = self._get_dict_value(_dict, fallback)
                if value:
                    break

        if value and list_value:
            self._add_list_triple(subject, predicate, value)
        elif value and date_value:
            self._add_date_triple(subject, predicate, value)
        elif value:
            # Normal text value
            self.g.add((subject, predicate, Literal(value)))

    def _add_list_triple(self, subject, predicate, value):
        '''
        Adds as many triples to the graph as values

        Values are literal strings, if `value` is a list, one for each
        item. If `value` is a string there is an attempt to split it using
        commas, to support legacy fields.
        '''
        items = []
        # List of values

        if isinstance(value, list):
            items = value
        elif isinstance(value, basestring):
            try:
                # JSON list
                value = json.loads(value)
            except ValueError:
                if ',' in value:
                    # Comma-separated list
                    items = value.split(',')
                else:
                    # Normal text value
                    items = [value]

        for item in items:
            self.g.add((subject, predicate, Literal(item)))

    def _add_date_triple(self, subject, predicate, value):
        '''
        Adds a new triple with a date object

        Dates are parsed using dateutil, and if the date obtained is correct,
        added to the graph as an XSD.dateTime value.

        If there are parsing errors, the literal string value is added.
        '''
        if not value:
            return
        try:
            default_datetime = datetime.datetime(1, 1, 1, 0, 0, 0)
            _date = parse_date(value, default=default_datetime)

            self.g.add((subject, predicate, Literal(_date.isoformat(),
                                                    datatype=XSD.dateTime)))
        except ValueError:
            self.g.add((subject, predicate, Literal(value)))

    def _last_catalog_modification(self):
        '''
        Returns the date and time the catalog was last modified

        To be more precise, the most recent value for `metadata_modified` on a
        dataset.

        Returns a dateTime string in ISO format, or None if it could not be
        found.
        '''

        result = toolkit.get_action('package_search')({}, {
            'sort': 'metadata_modified desc',
            'rows': 1,
        })
        if result and result.get('results'):
            return result['results'][0]['metadata_modified']
        return None


class EuropeanDCATAPProfile(RDFProfile):
    '''
    An RDF profile based on the DCAT-AP for data portals in Europe

    More information and specification:

    https://joinup.ec.europa.eu/asset/dcat_application_profile

    '''

    def parse_dataset(self, dataset_dict, dataset_ref):

        dataset_dict['tags'] = []
        dataset_dict['extras'] = []
        dataset_dict['resources'] = []

        # Basic fields
        for key, predicate in (
                ('title', DCT.title),
                ('notes', DCT.description),
                ('url', DCAT.landingPage),
                ):
            value = self._object_value(dataset_ref, predicate)
            if value:
                dataset_dict[key] = value

        # Tags
        keywords = self._object_value_list(dataset_ref, DCAT.keyword) or []
        for keyword in keywords:
            dataset_dict['tags'].append({'name': keyword})

        # Extras

        #  Simple values
        for key, predicate in (
                ('issued', DCT.issued),
                ('modified', DCT.modified),
                ('identifier', DCT.identifier),
                ('alternate_identifier', ADMS.identifier),
                ('dcat_version', ADMS.version),
                ('version_notes', ADMS.versionNotes),
                ('frequency', DCT.accrualPeriodicity),
                ('spatial_uri', DCT.spatial),
                ):
            value = self._object_value(dataset_ref, predicate)
            if value:
                dataset_dict['extras'].append({'key': key, 'value': value})

        #  Lists
        for key, predicate in (
                ('language', DCT.language),
                ('theme', DCAT.theme),
                ('conforms_to', DCAT.conformsTo),
                ):
            values = self._object_value_list(dataset_ref, predicate)
            if values:
                dataset_dict['extras'].append({'key': key,
                                               'value': json.dumps(values)})

        # Contact details
        contact = self._contact_details(dataset_ref, ADMS.contactPoint)
        for key in ('uri', 'name', 'email'):
            if contact.get(key):
                dataset_dict['extras'].append(
                    {'key': 'contact_{0}'.format(key),
                     'value': contact.get(key)})

        # Publisher
        publisher = self._publisher(dataset_ref, DCT.publisher)
        for key in ('uri', 'name', 'email', 'url', 'type'):
            if publisher.get(key):
                dataset_dict['extras'].append(
                    {'key': 'publisher_{0}'.format(key),
                     'value': publisher.get(key)})

        # Temporal
        start, end = self._time_interval(dataset_ref, DCT.temporal)
        if start:
            dataset_dict['extras'].append(
                {'key': 'temporal_start', 'value': start})
        if end:
            dataset_dict['extras'].append(
                {'key': 'temporal_end', 'value': end})

        # Dataset URI (explicitly show the missing ones)
        dataset_uri = (unicode(dataset_ref)
                       if isinstance(dataset_ref, rdflib.term.URIRef)
                       else None)
        dataset_dict['extras'].append({'key': 'uri', 'value': dataset_uri})

        # Resources
        for distribution in self._distributions(dataset_ref):

            resource_dict = {}

            #  Simple values
            for key, predicate in (
                    ('name', DCT.title),
                    ('description', DCT.description),
                    ('format', DCT['format']),
                    ('mimetype', DCAT.mediaType),
                    ('download_url', DCAT.downloadURL),
                    ('issued', DCT.issued),
                    ('modified', DCT.modified),
                    ('status', ADMS.status),
                    ('rights', DCT.rights),
                    ('license', DCT.license),
                    ):
                value = self._object_value(distribution, predicate)
                if value:
                    resource_dict[key] = value

            resource_dict['url'] = (self._object_value(distribution,
                                                       DCAT.accessURL) or
                                    self._object_value(distribution,
                                                       DCAT.downloadURL))

            size = self._object_value_int(distribution, DCAT.byteSize)
            if size is not None:
                resource_dict['size'] = size

            # Distribution URI (explicitly show the missing ones)
            resource_dict['uri'] = (unicode(distribution)
                                    if isinstance(distribution,
                                                  rdflib.term.URIRef)
                                    else None)

            dataset_dict['resources'].append(resource_dict)

        if self.compatibility_mode:
            # Tweak the resulting dict to make it compatible with previous
            # versions of the ckanext-dcat parsers
            for extra in dataset_dict['extras']:
                if extra['key'] in ('issued', 'modified', 'publisher_name',
                                    'publisher_email',):

                    extra['key'] = 'dcat_' + extra['key']

                if extra['key'] == 'language':
                    extra['value'] = ','.join(
                        sorted(json.loads(extra['value'])))

        return dataset_dict

    def graph_from_dataset(self, dataset_dict, dataset_ref):

        g = self.g

        for prefix, namespace in namespaces.iteritems():
            g.bind(prefix, namespace)

        g.add((dataset_ref, RDF.type, DCAT.Dataset))

        # rec.add((uri, DC.identifier, Literal(data["name"])))
        # ('spatial_uri', DCT.spatial),

        # Basic fields
        items = [
            ('title', DCT.title, None),
            ('notes', DCT.description, None),
            ('url', DCAT.landingPage, None),
            ('identifier', DCT.identifier, ['guid', 'id']),
            ('dcat_version', ADMS.version, ['version']),
            ('alternate_identifier', ADMS.identifier, None),
            ('version_notes', ADMS.versionNotes, None),
            ('frequency', DCT.accrualPeriodicity, None),

        ]
        self._add_triples_from_dict(dataset_dict, dataset_ref, items)

        # Tags
        for tag in dataset_dict.get('tags', []):
            g.add((dataset_ref, DCAT.keyword, Literal(tag['name'])))

        # Dates
        items = [
            ('issued', DCT.issued, ['metadata_created']),
            ('modified', DCT.modified, ['metadata_modified']),
        ]
        self._add_date_triples_from_dict(dataset_dict, dataset_ref, items)

        #  Lists
        items = [
            ('language', DCT.language, None),
            ('theme', DCAT.theme, None),
            ('conforms_to', DCAT.conformsTo, None),
        ]
        self._add_list_triples_from_dict(dataset_dict, dataset_ref, items)

        # Contact details
        if any([
            self._get_dataset_value(dataset_dict, 'contact_uri'),
            self._get_dataset_value(dataset_dict, 'contact_name'),
            self._get_dataset_value(dataset_dict, 'contact_email'),
            self._get_dataset_value(dataset_dict, 'maintainer'),
            self._get_dataset_value(dataset_dict, 'maintainer_email'),
            self._get_dataset_value(dataset_dict, 'author'),
            self._get_dataset_value(dataset_dict, 'author_email'),
        ]):

            contact_uri = self._get_dataset_value(dataset_dict, 'contact_uri')
            if contact_uri:
                contact_details = URIRef(contact_uri)
            else:
                contact_details = BNode()

            g.add((contact_details, RDF.type, VCARD.Organization))
            g.add((dataset_ref, ADMS.contactPoint, contact_details))

            items = [
                ('contact_name', VCARD.fn, ['maintainer', 'author']),
                ('contact_email', VCARD.hasEmail, ['maintainer_email',
                                                   'author_email']),
            ]

            self._add_triples_from_dict(dataset_dict, contact_details, items)

        # Publisher
        if any([
            self._get_dataset_value(dataset_dict, 'publisher_uri'),
            self._get_dataset_value(dataset_dict, 'publisher_name'),
            dataset_dict.get('organization'),
        ]):

            publisher_uri = publisher_uri_from_dataset_dict(dataset_dict)
            if publisher_uri:
                publisher_details = URIRef(publisher_uri)
            else:
                # No organization nor publisher_uri
                publisher_details = BNode()

            g.add((publisher_details, RDF.type, FOAF.Organization))
            g.add((dataset_ref, DCT.publisher, publisher_details))

            publisher_name = self._get_dataset_value(dataset_dict, 'publisher_name')
            if not publisher_name and dataset_dict.get('organization'):
                publisher_name = dataset_dict['organization']['title']

            g.add((publisher_details, FOAF.name, Literal(publisher_name)))
            # TODO: It would make sense to fallback these to organization
            # fields but they are not in the default schema and the
            # `organization` object in the dataset_dict does not include
            # custom fields
            items = [
                ('publisher_email', FOAF.mbox, None),
                ('publisher_url', FOAF.homepage, None),
                ('publisher_type', DCT.type, None),
            ]

            self._add_triples_from_dict(dataset_dict, publisher_details, items)

        # Temporal
        start = self._get_dataset_value(dataset_dict, 'temporal_start')
        end = self._get_dataset_value(dataset_dict, 'temporal_end')
        if start or end:
            temporal_extent = BNode()

            g.add((temporal_extent, RDF.type, DCT.PeriodOfTime))
            if start:
                self._add_date_triple(temporal_extent, SCHEMA.startDate, start)
            if end:
                self._add_date_triple(temporal_extent, SCHEMA.endDate, end)
            g.add((dataset_ref, DCT.temporal, temporal_extent))

        # Resources
        for resource_dict in dataset_dict.get('resources', []):

            distribution = URIRef(resource_uri(resource_dict))

            g.add((dataset_ref, DCAT.distribution, distribution))

            g.add((distribution, RDF.type, DCAT.Distribution))

            #  Simple values
            items = [
                ('name', DCT.title, None),
                ('description', DCT.description, None),
                # TODO: check
                ('format', DCT['format'], None),
                ('mimetype', DCAT.mediaType, None),
                ('status', ADMS.status, None),
                ('rights', DCT.rights, None),
                ('license', DCT.license, None),
            ]

            self._add_triples_from_dict(resource_dict, distribution, items)

            # URL
            url = resource_dict.get('url')
            download_url = resource_dict.get('download_url')
            if download_url:
                g.add((distribution, DCAT.downloadURL, Literal(download_url)))
            if (url and not download_url) or (url and url != download_url):
                g.add((distribution, DCAT.accessURL, Literal(url)))

            # Dates
            items = [
                ('issued', DCT.issued, None),
                ('modified', DCT.modified, None),
            ]

            self._add_date_triples_from_dict(resource_dict, distribution, items)

            # Int
            if resource_dict.get('size'):
                try:
                    g.add((distribution, DCAT.byteSize,
                           Literal(float(resource_dict['size']),
                                   datatype=XSD.decimal)))
                except TypeError:
                    g.add((distribution, DCAT.byteSize,
                           Literal(resource_dict['size'])))

    def graph_from_catalog(self, catalog_dict, catalog_ref):

        g = self.g

        for prefix, namespace in namespaces.iteritems():
            g.bind(prefix, namespace)

        g.add((catalog_ref, RDF.type, DCAT.Catalog))

        # Basic fields
        items = [
            ('title', DCT.title, config.get('ckan.site_title')),
            ('description', DCT.description, config.get('ckan.site_description')),
            ('homepage', FOAF.homepage, config.get('ckan.site_url')),
            ('language', DCT.language, config.get('ckan.locale_default', 'en')),
        ]
        for item in items:
            key, predicate, fallback = item
            value = catalog_dict.get(key, fallback)
            if value:
                g.add((catalog_ref, predicate, Literal(value)))

        # Dates
        modified = self._last_catalog_modification()
        if modified:
            self._add_date_triple(catalog_ref, DCT.modified, modified)